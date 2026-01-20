"""Setup configuration for LoRa WiFi Forwarder."""

import sys
import os

# Only launch CLI if run directly by user in interactive terminal
# Check if stdin is a TTY (interactive) and we're being run directly
if (__name__ == "__main__" and 
    hasattr(sys.stdin, 'isatty') and sys.stdin.isatty() and
    os.path.basename(sys.argv[0]) in ("setup.py", "__main__.py")):
    
    setup_commands = {
        "install", "build", "sdist", "bdist", "bdist_wheel", "bdist_egg",
        "develop", "test", "check", "upload", "register", "clean", "egg_info",
        "build_ext", "build_py", "build_clib", "build_scripts", "--help", "--help-commands"
    }
    
    # If no args or first arg is not a setup command, launch CLI
    if len(sys.argv) == 1 or (len(sys.argv) > 1 and sys.argv[1] not in setup_commands):
        try:
            from cli import main as cli_main
            cli_main()
            sys.exit(0)
        except ImportError as e:
            print("Error: Could not import CLI module.")
            print(f"Details: {e}")
            print("\nMake sure you're running from the project directory.")
            print("To install the package: pip install -e .")
            print("Then run: lora-wifi-cli")
            sys.exit(1)

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
            "lora-wifi-cli=cli:main",
            "meshtastic-test=testing_tool:main",
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
