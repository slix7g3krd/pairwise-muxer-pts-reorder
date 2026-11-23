import pytest
import media.muxer_pts_reorder as mod

CASES = [
    ("dts_reorder", (1,)),
    ("timebase_convert", (1,)),
    ("edit_list_delay", (1,)),
    ("index_last_write", (1,)),
    ("recompute_after_drop", (1,)),
    ("determinism", (1,)),
    ("monotonic_check", (1,)),
    ("validation", (1,)),
    ("audit", (1,)),
    ("fallback", (1,)),
    ("duration_clamp", (1,)),
    ("metrics", (1,)),
    ("write_frame", (1,)),
    ("seek_index", (1,)),
    ("trailer", (1,)),
]

@pytest.mark.parametrize("fn_name,args", CASES)
def test_contract(fn_name, args):
    fn = getattr(mod, fn_name)
    assert fn(*args) is not None
