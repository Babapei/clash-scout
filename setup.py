"""兼容旧版 setuptools（<61）的安装入口。

新版用 pyproject.toml（PEP 621），旧版读不到那个段，所以这里显式声明一遍。
两边保持同步即可。
"""

from setuptools import find_packages, setup

setup(
    name="clash-ip-tracker",
    version="0.3.0",
    description="记录代理节点域名 IP 池的健康状态，找出值得手动指定的 IP",
    packages=find_packages(include=["clash_ip_tracker*"]),
    python_requires=">=3.9",
    install_requires=["PyYAML>=6"],
    entry_points={
        "console_scripts": [
            "scout=clash_ip_tracker.cli:main",
        ],
    },
)
