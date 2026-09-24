"""Compatibility metadata for Python 3.9 environments with pre-PEP-660 pip."""

from setuptools import find_packages, setup


setup(
    name="github-account-analysis",
    version="0.1.0",
    description="Reproducible, bounded analysis of public GitHub repository and event data.",
    python_requires=">=3.9",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    install_requires=[
        "duckdb>=1.1,<2",
        "jinja2>=3.1,<4",
        "matplotlib>=3.7,<4",
        "pypdf>=5,<7",
        "weasyprint>=62,<70",
    ],
    extras_require={"test": ["pytest>=8,<9"]},
    entry_points={"console_scripts": ["github-account-analysis=github_account_analysis.cli:main"]},
)
