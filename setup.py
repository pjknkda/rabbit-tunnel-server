from os import path

from setuptools import find_packages, setup

wdir = path.abspath(path.dirname(__file__))

try:
    with open(path.join(wdir, 'README.md'), encoding='utf-8') as f:
        long_description = f.read()
except FileNotFoundError:
    long_description = ''

install_requires = [
    'async-timeout==3.0.1',
    'httptools==0.2.0',
    'msgpack==1.0.2',
    'setuptools-scm==6.0.1',
    'starlette==0.15.0',
    'uvicorn==0.14.0',
    'uvloop==0.15.2; platform_system != "Windows"',
    'websockets==9.1',
]

dev_install_requires = [
    'autopep8==1.5.7',
    'bandit==1.7.0',
    'flake8-bugbear==21.4.3',
    'flake8-datetimez==20.10.0',
    'flake8-isort==4.0.0',
    'flake8-logging-format==0.6.0',
    'flake8-quotes==3.2.0',
    'flake8==3.9.2',
    'isort==5.9.2',
    'mypy==0.910',
    'pip-tools==6.2.0',
    'pytest-cov==2.12.1',
    'pytest-env==0.6.2',
    'pytest==6.2.4',
    'safety==1.10.3',
]


if __name__ in ('__main__', 'builtins'):
    setup(
        name='rabbit-tunnel-server',

        description='A server for rabbit tunnel',
        long_description=long_description,
        long_description_content_type='text/markdown',

        license='MIT License',

        url='https://github.com/pjknkda/rabbit-tunnel-server',

        author='Jungkook Park',
        author_email='pjknkda@gmail.com',

        classifiers=[
            'Development Status :: 5 - Production/Stable',
            'Programming Language :: Python :: 3.7',
            'Programming Language :: Python :: 3.8',
            'Programming Language :: Python :: 3.9',
            'Programming Language :: Python :: Implementation :: CPython',
            'Operating System :: POSIX',
            'Operating System :: MacOS :: MacOS X'
        ],

        packages=find_packages(),

        python_requires='>=3.7, <3.10',

        use_scm_version=True,
        setup_requires=['setuptools-scm'],

        install_requires=install_requires,
        extras_require={'dev': dev_install_requires},

        entry_points={
            'console_scripts': [
                'rt-server = rabbit_tunnel_server.__main__:main',
            ],
        },

        package_data={}
    )
