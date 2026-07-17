""" unit tests for each stage of offline_debug.py using frozen fixture in tests/fixtures/offline_debug/"""

import pandas as pd
from scripts.analysis.offline_debug import (setup, _load_static, _stage_extract, _stage_project, _stage_upsample, _stage_group, _stage_label, _stage_diff)


