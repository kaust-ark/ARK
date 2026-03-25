from setuptools import setup, find_packages

setup(
    name="ark-research",
    version="0.1.0",
    packages=find_packages(include=["ark*"]),
    package_data={
        "ark": ["templates/**/*", "webapp/static/*", "webapp/slurm_template.sh"],
    },
    install_requires=["pyyaml>=6.0"],
    extras_require={
        "dashboard": [
            "fastapi>=0.100",
            "uvicorn[standard]>=0.20",
        ],
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
    python_requires=">=3.9",
)
