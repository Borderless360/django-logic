from django.test import TestCase

from demo.process import LockerProcess
from demo.models import Lock


class User:
    def __init__(self, is_staff=False):
        self.is_staff = is_staff


class LockProcessTestCase(TestCase):
    def setUp(self):
        self.user = User()
        self.process_class = LockerProcess

    def test_process_class_method(self):
        self.assertEqual(self.process_class.process_name, 'process')

    def test_user_lock_process(self):
        lock = Lock.objects.create()
        self.assertEqual(lock.status, 'open')
        lock.process.lock(user=self.user)
        self.assertEqual(lock.status, 'locked')
        lock.process.unlock(user=self.user)
        self.assertEqual(lock.status, 'open')
