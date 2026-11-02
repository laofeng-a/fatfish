import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('memory_reader',Path(__file__).with_name('memory-reader.py'))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
NAMES=[item['name'] for item in module.TOOLS]
