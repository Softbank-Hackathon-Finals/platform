"""Regression cases for source preservation and explicit attachment inventory."""
import importlib.util
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / 'infra/envs/target-workloads/sync_attachments.py'
SPEC = importlib.util.spec_from_file_location('target_attachments', PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Store:
    def __init__(self, values):
        self.values = values.copy()
        self.writes = []

    def list_keys(self):
        return list(self.values)

    def read(self, key):
        return self.values[key]

    def create(self, key, value):
        if key in self.values:
            raise ValueError('conditional create conflict')
        self.values[key] = value
        self.writes.append(key)


class AttachmentPreservationTests(unittest.TestCase):
    def test_copy_and_identical_retry_without_source_write(self):
        source, target = Store({'attachment': b'data'}), Store({})
        result = MODULE.synchronize(source, target, ['attachment'])
        self.assertEqual((result['copied'], result['reused']), (1, 0))
        retry = MODULE.synchronize(source, target, ['attachment'])
        self.assertEqual((retry['copied'], retry['reused']), (0, 1))
        self.assertEqual(source.writes, [])
        self.assertEqual(target.values, source.values)
        self.assertEqual(result['key_content_inventory_sha256'], retry['key_content_inventory_sha256'])

    def test_default_rejects_extra_source_before_target_write(self):
        target = Store({})
        with self.assertRaises(ValueError):
            MODULE.synchronize(Store({'attachment': b'a', 'unknown': b'b'}), target, ['attachment'])
        self.assertEqual(target.writes, [])

    def test_individually_approved_extra_is_counted_separately(self):
        result = MODULE.synchronize(Store({'attachment': b'a', 'known': b'b'}), Store({}),
                                    ['attachment'], approved_extra_keys=['known'])
        self.assertEqual((result['database_referenced_count'], result['operator_approved_extra_count']), (1, 1))
        self.assertEqual((result['source_count'], result['destination_count']), (2, 2))

    def test_approved_extra_is_not_a_wildcard(self):
        target = Store({})
        with self.assertRaises(ValueError):
            MODULE.synchronize(Store({'attachment': b'a', 'known': b'b', 'new': b'c'}), target,
                               ['attachment'], approved_extra_keys=['known'])
        self.assertEqual(target.writes, [])

    def test_approved_and_referenced_overlap_is_invalid(self):
        with self.assertRaises(ValueError):
            MODULE.synchronize(Store({'a': b'a'}), Store({}), ['a'], approved_extra_keys=['a'])

    def test_unrelated_destination_object_is_preserved(self):
        target = Store({'unrelated': b'keep'})
        with self.assertRaises(ValueError):
            MODULE.synchronize(Store({'attachment': b'a'}), target, ['attachment'])
        self.assertEqual(target.values, {'unrelated': b'keep'})
        self.assertEqual(target.writes, [])

    def test_conflicting_existing_bytes_are_preserved(self):
        target = Store({'a': b'keep'})
        with self.assertRaises(ValueError):
            MODULE.synchronize(Store({'a': b'new'}), target, ['a'])
        self.assertEqual(target.values, {'a': b'keep'})
        self.assertEqual(target.writes, [])

    def test_source_change_cannot_report_success(self):
        class Changing(Store):
            reads = 0

            def read(self, key):
                self.reads += 1
                return b'first' if self.reads == 1 else b'changed'

        with self.assertRaises(ValueError):
            MODULE.synchronize(Changing({'a': b'first'}), Store({}), ['a'])

    def test_destination_corruption_cannot_report_success(self):
        class Corrupt(Store):
            def create(self, key, value):
                super().create(key, b'corrupt')

        with self.assertRaises(ValueError):
            MODULE.synchronize(Store({'a': b'expected'}), Corrupt({}), ['a'])

    def test_content_swapped_between_keys_changes_inventory_digest(self):
        one = MODULE.synchronize(Store({'a': b'1', 'b': b'2'}), Store({}), ['a', 'b'])
        two = MODULE.synchronize(Store({'a': b'2', 'b': b'1'}), Store({}), ['a', 'b'])
        self.assertEqual(one['content_sha256'], two['content_sha256'])
        self.assertNotEqual(one['key_content_inventory_sha256'], two['key_content_inventory_sha256'])


if __name__ == '__main__':
    unittest.main()
