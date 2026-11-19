import setuptools
version = '2.1.0'

setuptools.setup(
    name='electrumX',
    version=version,
    scripts=['electrumx_server', 'electrumx_rpc', 'electrumx_compact_history'],
    python_requires='>=3.12',
    install_requires=['aiorpcX[ws]>=0.25,<0.26', 'attrs',
                      'plyvel>=1.5.1', 'pylru', 'aiohttp>=3.14.5,<4'],
    extras_require={
        'rocksdb': ['python-rocksdb>=0.6.9'],
        'uvloop': ['uvloop>=0.12.2'],   # Bump when the uvloop connection_lost bug is fixed
        # For various coins
        'blake256': ['blake256>=0.1.1'],
        'crypto': ['pycryptodomex>=3.8.1'],
        'groestl': ['groestlcoin-hash>=1.0.1'],
        'tribus-hash': ['tribus-hash>=1.0.2'],
        'xevan-hash': ['xeven-hash'],
        'x11-hash': ['x11-hash>=1.4'],
        'zny-yespower-0-5': ['zny-yespower-0-5'],
        'bell-yespower': ['bell-yespower'],
        'cpupower': ['cpupower'],
    },
    packages=setuptools.find_packages(include=('electrumx*',)),
    description='ElectrumX Server',
    author='Neil Booth',
    author_email='kyuupichan@gmail.com',
    license='MIT Licence',
    url='https://github.com/bitcoinvault/electrumx',
    long_description='Server implementation for the Electrum protocol',
    download_url=('https://github.com/bitcoinvault/electrumx/archive/'
                  f'{version}.tar.gz'),
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Framework :: AsyncIO',
        'License :: OSI Approved :: MIT License',
        'Operating System :: Unix',
        "Programming Language :: Python :: 3.12",
        "Topic :: Database",
        'Topic :: Internet',
    ],
)
