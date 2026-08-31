import unittest

from atlas import hooks


class TestApp(unittest.TestCase):
	def test_app_metadata(self):
		self.assertEqual(hooks.app_name, "atlas")
		self.assertTrue(hooks.export_python_type_annotations)
