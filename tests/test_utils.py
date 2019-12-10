from django.test import TestCase

from django_logic.utils import convert_to_snake_case, convert_to_readable_name


class SnakeCaseTestCase(TestCase):
    def test_convert_to_snake_case(self):
        self.assertEqual(convert_to_snake_case('TestCase'), 'test_case')
        self.assertEqual(convert_to_snake_case('Test'), 'test')
        self.assertEqual(convert_to_snake_case('CamelTestCase'), 'camel_test_case')


class ReadableFormatTestCase(TestCase):
    def test_convert_to_readable_name(self):
        self.assertEqual(convert_to_readable_name('TestCase'), 'Test Case')
        self.assertEqual(convert_to_readable_name('Test'), 'Test')
        self.assertEqual(convert_to_readable_name('CamelTestCase'), 'Camel Test Case')
