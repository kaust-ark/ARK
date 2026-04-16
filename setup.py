from setuptools import setup, find_packages

setup(
    name="ark-research",
    version="0.1.1",
    packages=find_packages(include=["ark*", "website*"]),
    package_data={
        "ark": ["templates/**/*"],
        "website": ["dashboard/static/*", "dashboard/templates/*", "dashboard/slurm_template.sh"],
    },
    install_requires=["pyyaml>=6.0"],
    extras_require={
        "webapp": [
            "fastapi>=0.100",
            "uvicorn[standard]>=0.20",
            "sqlmodel>=0.0.14",
            "authlib>=1.3",
            "httpx>=0.25",
            "python-multipart>=0.0.6",
            "jinja2>=3.1",
            "python-dotenv>=1.0",
            "itsdangerous>=2.1",
        ],
    },
    entry_points={
        "console_scripts": [
            "ark=ark.cli:main",
        ],
    },
    python_requires=">=3.10",
)
