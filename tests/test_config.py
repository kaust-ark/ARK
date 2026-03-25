"""Tests for ark.latex_geometry."""

import json
from pathlib import Path

from ark.latex_geometry import (
    get_geometry,
    get_matplotlib_rcparams,
    write_figure_config,
    VENUE_PRESETS,
    VENUE_ALIASES,
)


class TestGetGeometry:
    def test_known_venue(self):
        geo = get_geometry("acmart-sigplan")
        assert geo["columnwidth_in"] == 3.333
        assert geo["textwidth_in"] == 7.0
        assert geo["font_size_pt"] == 10

    def test_alias(self):
        geo = get_geometry("sigplan")
        assert geo == get_geometry("acmart-sigplan")

    def test_euromlsys_alias(self):
        geo = get_geometry("euromlsys")
        assert geo["columnwidth_in"] == 3.333  # same as sigplan

    def test_fallback_unknown(self):
        geo = get_geometry("unknown-venue-xyz")
        # Should fall back to acmart-sigplan
        assert geo["columnwidth_in"] == 3.333

    def test_case_insensitive(self):
        geo = get_geometry("IEEE")
        assert geo["columnwidth_in"] == 3.5

    def test_returns_copy(self):
        """get_geometry should return a copy, not a reference to the preset."""
        geo = get_geometry("neurips")
        geo["columnwidth_in"] = 999
        geo2 = get_geometry("neurips")
        assert geo2["columnwidth_in"] == 5.5


class TestMatplotlibRcParams:
    def test_font_size(self):
        geo = get_geometry("acmart-sigplan")
        rc = get_matplotlib_rcparams(geo)
        assert rc["font.size"] == 10
        assert rc["axes.labelsize"] == 10

    def test_figure_dpi(self):
        geo = get_geometry("ieee")
        rc = get_matplotlib_rcparams(geo)
        assert rc["figure.dpi"] == 300


class TestWriteFigureConfig:
    def test_writes_json(self, tmp_figures_dir):
        geo = get_geometry("acmart-sigplan")
        out = tmp_figures_dir / "figure_config.json"
        write_figure_config(geo, out)
        assert out.exists()

        data = json.loads(out.read_text())
        assert "geometry" in data
        assert "matplotlib_rcparams" in data
        assert "sizes" in data
        assert data["geometry"]["columnwidth_in"] == 3.333

    def test_creates_parent_dirs(self, tmp_path):
        out = tmp_path / "nested" / "dir" / "config.json"
        geo = get_geometry("neurips")
        write_figure_config(geo, out)
        assert out.exists()
