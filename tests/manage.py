#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(os.path.dirname(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def main():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tests.settings')
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
