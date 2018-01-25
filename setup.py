"""Installer for conn-check
"""

import os
cwd = os.path.dirname(__file__)
__version__ = open(os.path.join(cwd, 'conn_check/version.txt'),
                   'r').read().strip()

from setuptools import setup, find_packages


def get_requirements(*pre):
    extras = []

    # Base requirements
    if not pre:
        pre = ('',)

    for p in pre:
        sep = '-' if p else ''
        extras.extend(open('{}{}requirements.txt'.format(p, sep)).readlines())
    return extras


setup(
    name='conn-check',
    description='Utility for verifying connectivity between services',
    long_description=open('README.rst').read(),
    version=__version__,
    author='James Westby, Wes Mason',
    author_email='james.westby@canonical.com, wesley.mason@canonical.com',
    url='http://conn-check.org/',
    packages=find_packages(exclude=['ez_setup']),
    #install_requires=get_requirements(),
    extras_require={
        'all': get_requirements('amqp', 'postgres', 'mongodb',
                                'fwutils'),
        'amqp': get_requirements('amqp'),
        'postgres': get_requirements('postgres'),
        # 'redis': get_requirements('redis'),
        'mongodb': get_requirements('mongodb'),
        'fwutil': get_requirements('fwutils'),
    },
    package_data={'conn_check': ['version.txt', 'amqp0-8.xml']},
    include_package_data=True,
    entry_points={
        'console_scripts': [
            'conn-check = conn_check.main:main',
            'conn-check-export-fw = conn_check.utils.firewall_rules:main',
            'conn-check-convert-fw = conn_check.utils.convert_fw_rules:main',
        ],
    },
    license='GPL3',
    classifiers=[
        "Topic :: System :: Networking",
        "Development Status :: 4 - Beta",
        "Programming Language :: Python",
        "Intended Audience :: Developers",
        "Operating System :: OS Independent",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
    ]
)
