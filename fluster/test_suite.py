# fluster - testing framework for codecs
# Copyright (C) 2020, Fluendo, S.A.
#  Author: Pablo Marcos Oltra <pmarcos@fluendo.com>, Fluendo, S.A.
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Library General Public
# License as published by the Free Software Foundation; either
# version 2 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Library General Public License for more details.
#
# You should have received a copy of the GNU Library General Public
# License along with this library; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place - Suite 330,
# Boston, MA 02111-1307, USA.

import os.path
import json
import unittest
import copy
from multiprocessing import Pool
from unittest.result import TestResult
from time import perf_counter
from shutil import rmtree

from fluster.test_vector import TestVector
from fluster.codec import Codec
from fluster.decoder import Decoder
from fluster.test import Test
from fluster import utils


class DownloadWork:
    '''Context to pass to each download worker'''
    # pylint: disable=too-few-public-methods

    def __init__(self, out_dir: str, verify: bool, extract_all: bool, keep_file: bool,
                 test_suite_name: str, test_vector: TestVector):
        self.out_dir = out_dir
        self.verify = verify
        self.extract_all = extract_all
        self.keep_file = keep_file
        self.test_suite_name = test_suite_name
        self.test_vector = test_vector


class TestSuite:
    '''Test suite class'''

    def __init__(self, filename: str, resources_dir: str, name: str, codec: Codec, description: str,
                 test_vectors: list):
        # Not included in JSON
        self.filename = filename
        self.resources_dir = resources_dir

        # JSON members
        self.name = name
        self.codec = codec
        self.description = description
        self.test_vectors = test_vectors

    def clone(self):
        '''Create a deep copy of the object'''
        return copy.deepcopy(self)

    @classmethod
    def from_json_file(cls, filename: str, resources_dir: str):
        '''Create a TestSuite instance from a file'''
        with open(filename) as json_file:
            data = json.load(json_file)
            data['test_vectors'] = list(
                map(TestVector.from_json, data["test_vectors"]))
            data['codec'] = Codec(data['codec'])
            return cls(filename, resources_dir, **data)

    def to_json_file(self, filename: str):
        '''Serialize the test suite to a file'''
        with open(filename, 'w') as json_file:
            data = self.__dict__.copy()
            data.pop('resources_dir')
            data.pop('filename')
            data['codec'] = str(self.codec.value)
            data['test_vectors'] = [tv.data_to_serialize()
                                    for tv in self.test_vectors]
            json.dump(data, json_file, indent=4)

    def _download_worker(self, context: DownloadWork):
        '''Download and extract a test vector'''
        test_vector = context.test_vector
        dest_dir = os.path.join(
            context.out_dir, context.test_suite_name, test_vector.name)
        dest_path = os.path.join(
            dest_dir, os.path.basename(test_vector.source))
        if not os.path.exists(dest_dir):
            os.makedirs(dest_dir)
        file_downloaded = os.path.exists(dest_path)
        if file_downloaded and context.verify:
            if test_vector.source_checksum != utils.file_checksum(dest_path):
                file_downloaded = False
        print(f'\tDownloading test vector {test_vector.name} from {dest_dir}')
        utils.download(test_vector.source, dest_dir)
        if utils.is_extractable(dest_path):
            print(
                f'\tExtracting test vector {test_vector.name} to {dest_dir}')
            utils.extract(
                dest_path, dest_dir, file=test_vector.input_file if not context.extract_all else None)
            if not context.keep_file:
                os.remove(dest_path)

    def download(self, jobs: int, out_dir: str, verify: bool, extract_all: bool = False, keep_file: bool = False):
        '''Download the test suite'''
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)
        print(f'Downloading test suite {self.name} using {jobs} parallel jobs')
        download_tasks = []
        for test_vector in self.test_vectors:
            download_tasks.append(
                DownloadWork(out_dir, verify, extract_all, keep_file, self.name, test_vector))

        with Pool(jobs) as pool:
            pool.map(self._download_worker, download_tasks, chunksize=1)

        print('All downloads finished')

    def _run_worker(self, test: Test):
        '''Run one unit test returning the TestVector'''
        test_result = TestResult()
        test(test_result)
        line = '.'
        if not test_result.wasSuccessful():
            line = 'x'
        print(line, end='', flush=True)
        if test_result.failures:
            test.test_vector.errors += test_result.failures
        if test_result.errors:
            test.test_vector.errors += test_result.errors
        return test.test_vector

    def run_test_suite_sequentially(self, tests: list, failfast: bool, quiet: bool):
        '''Run the test suite sequentially'''
        suite = unittest.TestSuite()
        suite.addTests(tests)
        runner = unittest.TextTestRunner(
            failfast=failfast, verbosity=1 if quiet else 2)
        res = runner.run(suite)

        # Collect all TestResults with error to add them into the test vectors
        for test_result in res.failures:
            test_vector = test_result[0].test_vector
            test_vector.errors.append(test_result[1])
        for test_result in res.errors:
            test_vector = test_result[0].test_vector
            test_vector.errors.append(test_result[1])

    def run_test_suite_in_parallel(self, jobs: int, tests: list):
        '''Run the test suite in parallel'''
        with Pool(jobs) as pool:
            start = perf_counter()
            test_results = pool.map(self._run_worker, tests)
            print('\n')
            end = perf_counter()
            success = 0
            for test_vector_res in test_results:
                if test_vector_res.errors:
                    for failure in test_vector_res.errors:
                        for line in failure:
                            print(line)
                else:
                    success += 1

                # Collect the test vector results and failures since they come
                # from a different process
                for tvector in self.test_vectors:
                    if tvector.name == test_vector_res.name:
                        tvector.result = test_vector_res.result
                        if test_vector_res.errors:
                            tvector.errors = test_vector_res.errors
                        break
            print(
                f'Ran {success}/{len(test_results)} tests successfully in {end-start:.3f} secs')

    def run(self, jobs: int, decoder: Decoder, timeout: int, failfast: bool, quiet: bool, results_dir: str,
            reference: bool = False, test_vectors: list = None, keep_files: bool = False):
        '''
        Run the test suite.
        Returns a new copy of the test suite with the result of the test
        '''
        # pylint: disable=too-many-locals

        # decoders using hardware acceleration cannot be easily parallelized
        # reliably and may case issues. Thus, we execute them sequentially
        if decoder.hw_acceleration and jobs > 1:
            jobs = 1
            print(
                f'Decoder {decoder.name} uses hardware acceleration, using 1 job automatically')

        print('*' * 100 + '\n')
        string = f'Running test suite {self.name} with decoder {decoder.name}'
        if test_vectors:
            string += f' and test vectors {", ".join(test_vectors)}'
        string += f' using {jobs} parallel jobs'
        print(string)
        print('*' * 100 + '\n')
        if not decoder.check_run():
            print(f'Skipping decoder {decoder.name} because it cannot be run')
            return None

        results_dir = os.path.join(results_dir, self.name, 'test_results')
        if not os.path.exists(results_dir):
            os.makedirs(results_dir)

        test_suite = self.clone()
        tests = test_suite.generate_tests(
            decoder, results_dir, reference, test_vectors, timeout, keep_files)

        if jobs == 1:
            test_suite.run_test_suite_sequentially(
                tests, failfast, quiet)
        else:
            test_suite.run_test_suite_in_parallel(jobs, tests)

        if reference:
            test_suite.to_json_file(test_suite.filename)

        if not keep_files and os.path.isdir(results_dir):
            rmtree(results_dir)

        return test_suite

    def generate_tests(self, decoder: Decoder, results_dir: str, reference: bool, test_vectors: list,
                       timeout: int, keep_files: bool):
        '''Generate the tests for a decoder'''
        tests = []
        test_vectors_run = []
        for test_vector in self.test_vectors:
            if test_vectors:
                if test_vector.name.lower() not in test_vectors:
                    continue
            tests.append(
                Test(decoder, self, test_vector, results_dir, reference, timeout, keep_files))
            test_vectors_run.append(test_vector)
        self.test_vectors = test_vectors_run
        return tests

    def __str__(self):
        return f'\n{self.name}\n' \
            f'    Codec: {self.codec}\n' \
            f'    Description: {self.description}\n' \
            f'    Test vectors: {len(self.test_vectors)}'