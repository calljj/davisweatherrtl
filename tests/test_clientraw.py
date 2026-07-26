import os

import pytest

from davis_clientraw import clientraw
from davis_clientraw.state import StationState

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "templates")

CONFIG = {
    "station": {
        "name": "Bracklesham Boardriders",
        "timezone": "Europe/London",
        "latitude": 50.777,
        "longitude": -0.858,
    }
}


@pytest.fixture
def state(tmp_path):
    s = StationState(str(tmp_path / "state.sqlite"), "Europe/London")
    s.update_current(
        wind_speed_kt=8.5,
        gust_kt=12.0,
        wind_dir_deg=260,
        temp_c=20.0,
        humidity_pct=83,
        pressure_hpa=1028.2,
        rain_rate_mm_h=0.0,
        solar_wm2=100.0,
        uv_index=2.0,
    )
    s.append_minute_sample()
    return s


@pytest.mark.parametrize(
    "filename,generator",
    [
        ("clientraw.txt", clientraw.generate_clientraw),
        ("clientrawhour.txt", clientraw.generate_clientrawhour),
        ("clientrawdaily.txt", clientraw.generate_clientrawdaily),
        ("clientrawextra.txt", clientraw.generate_clientrawextra),
    ],
)
def test_generated_file_matches_template_shape(state, filename, generator):
    tmpl = clientraw.load_template(TEMPLATES_DIR, filename)
    out = generator(state, CONFIG, tmpl).split()
    assert len(out) == clientraw.EXPECTED_COUNTS[filename]
    assert out[-1] == clientraw.EXPECTED_MARKERS[filename]
    assert out[0] == "12345"


def test_clientraw_confirmed_fields_overwritten(state):
    tmpl = clientraw.load_template(TEMPLATES_DIR, "clientraw.txt")
    out = clientraw.generate_clientraw(state, CONFIG, tmpl).split()
    assert out[1] == "8.5"
    assert out[2] == "12.0"
    assert out[3] == "260"
    # Average wind direction (field 117, historically left as the
    # template's static placeholder -- some third-party consumers of
    # clientraw.txt read this field instead of the current-direction one
    # at index 3, and used to see a permanently frozen value).
    assert out[117] == "260"
    assert out[4] == "20.0"
    assert out[5] == "83"
    assert out[6] == "1028.2"
    assert out[14] == "100.0"  # soil_temperature placeholder
    assert out[32] == "Bracklesham_Boardriders"


def test_clientraw_unmapped_fields_keep_template_value(state):
    tmpl = clientraw.load_template(TEMPLATES_DIR, "clientraw.txt")
    out = clientraw.generate_clientraw(state, CONFIG, tmpl)
    out_tokens = out.split()
    # forecast_icon (index 15) is never overwritten -- must equal the template's own value.
    assert out_tokens[15] == tmpl[15]


def test_validate_tokens_rejects_wrong_field_count():
    with pytest.raises(ValueError):
        clientraw.validate_tokens("clientraw.txt", ["12345", "1", "!!C10.37S152!!"])


def test_validate_tokens_rejects_wrong_marker():
    tokens = ["12345"] + ["0"] * 176 + ["!!WRONG!!"]
    with pytest.raises(ValueError):
        clientraw.validate_tokens("clientraw.txt", tokens)
