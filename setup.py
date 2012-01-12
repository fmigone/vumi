from setuptools import setup, find_packages


def listify(filename):
    return filter(None, open(filename, 'r').read().split('\n'))


def remove_externals(requirements):
    return filter(lambda e: not e.startswith('-e'), requirements)

setup(
    name="vumi",
    version="0.4.0a",
    url='http://github.com/praekelt/vumi',
    license='BSD',
    description="Super-scalable messaging engine for the delivery of SMS, "
                "Star Menu and chat messages to diverse audiences in "
                "emerging markets and beyond.",
    long_description=open('README.rst', 'r').read(),
    author='Praekelt Foundation',
    author_email='dev@praekeltfoundation.org',
    packages=find_packages(),
    include_package_data=True,
    install_requires=['setuptools'] +
                     remove_externals(listify('requirements.pip')),

    dependency_links=[
        'https://github.com/dmaclay/python-smpp/zipball/develop#egg=python-smpp',
        'https://github.com/dustin/twitty-twister/zipball/master#egg=twitty-twister',
    ],

    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Operating System :: POSIX',
        'Programming Language :: Python',
        'Topic :: Software Development :: Libraries :: Python Modules',
        'Topic :: System :: Networking',
    ],
)
