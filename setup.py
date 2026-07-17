from setuptools import setup, find_packages

setup(
    name="onion-compress",
    version="0.1.0",
    description="Onion — Adaptive Layered Compression Engine with a searchable, self-describing archive wrapper",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="A. Hill",
    keywords=[
        "compression", "archive", "archiver", "encryption", "aes-256",
        "hmac", "signing", "metadata", "searchable-archive",
        "self-describing", "no-decompression-search", "lz77", "huffman",
        "lzma", "lz4", "delta-encoding", "deflate", "gzip-alternative",
        "table-of-contents", "toc", "backup-tool", "cli", "web-ui",
        "file-format", "data-wrapper",
    ],
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Environment :: Web Environment",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Programming Language :: Python :: 3",
        "Programming Language :: C",
        "Topic :: System :: Archiving :: Compression",
        "Topic :: System :: Archiving :: Backup",
        "Topic :: Security :: Cryptography",
        "Topic :: Database :: Front-Ends",
    ],
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "cryptography>=41.0",
    ],
    extras_require={
        "qt": ["PyQt6>=6.4"],
    },
    entry_points={
        "console_scripts": [
            "onion=ace.cli:main",
        ],
    },
)
