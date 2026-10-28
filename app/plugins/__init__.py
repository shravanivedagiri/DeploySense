from .docker import DockerPlugin
from .database import DatabasePlugin
from .general import GeneralPlugin

PLUGINS = [DockerPlugin(), DatabasePlugin(), GeneralPlugin()]
PLUGIN_BY_NAME = {p.name: p for p in PLUGINS}
