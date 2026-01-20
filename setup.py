"""Setup configuration for LoRa WiFi Forwarder."""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="lora-wifi-forwarder",
    version="0.1.0",
    description="LoRa WiFi Forwarder - HTTP proxy over Meshtastic LoRa mesh",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="LoRa WiFi Forwarder Team",
    python_requires=">=3.8",
    packages=find_packages(exclude=["tests"]),
    install_requires=[
        "meshtastic>=2.0.0",
        "pypubsub>=4.0.3",
        "pyserial>=3.5",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "clientd=client.daemon:main",
            "gatewayd=gateway.daemon:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Communications",
        "Topic :: Internet :: Proxy Servers",
    ],
)
