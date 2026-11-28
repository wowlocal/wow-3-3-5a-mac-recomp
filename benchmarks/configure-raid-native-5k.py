"""Apply the requested native 5K graphics profile to a copied raid client."""

import argparse
from pathlib import Path
import re


OPTIONS = {
    "gxWindow": 1, "gxMaximize": 0, "gxResolution": "5120x2880", "gxWindowedResolution": "5120x2880",
    "gxVSync": 0, "gxMultisample": 4, "gxMultisampleQuality": 1, "gxColorBits": 24, "gxDepthBits": 24,
    "maxFPS": 0, "maxFPSBk": 0, "farclip": 1277, "shadowLevel": 0, "particleDensity": 1,
    "environmentDetail": 1.5, "groundEffectDensity": 64, "groundEffectDist": 140, "baseMip": 0,
    "extShadowQuality": 5, "textureFilteringMode": 5, "weatherDensity": 3, "componentTextureLevel": 9,
    "specular": 1, "ffxGlow": 1, "ffxDeath": 1, "projectedTextures": 1,
}


def settings_for(contents):
    names = {name.casefold() for name in OPTIONS}
    directive = re.compile(r"^\s*SET\s+(\S+)\s", re.IGNORECASE)
    rows = [line for line in contents.splitlines()
            if not (match := directive.match(line)) or match[1].casefold() not in names]
    rows.extend(f'SET {key} "{value}"' for key, value in OPTIONS.items())
    return "\n".join(rows) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="WTF/Config.wtf in the isolated client")
    args = parser.parse_args()
    args.config.write_text(settings_for(args.config.read_text()))
