# Kentucky Fried Evan: Mech Royale bot

Our bot for the Builders Buildathon "Mech Royale" track (mechfight/1 protocol). Released under the MIT License (`LICENSE`).

- `kfe2.py`: the bot. Track-based engagement rules, retreat to cover when outnumbered (with a fewest-watchers fallback and a
  withdraw step), peek-aware fight positioning, bullet and laser dodging, latency-aware lead, lethal early laser release,
  cube grabs during fights, reachability-checked looting, and a separate free-for-all policy after detection.
- `worldmap.py`: our map model built from `/arena` at startup (terrain, building and bridge grids, line of sight, nav grid,
  A*, connected regions, roof interiors).
- `bot.py`: our first-generation bot (kept for comparison).
- `sim/`: our approximate local match simulator used for testing. Not used at runtime.
- `Dockerfile`: python:3.13-slim with numpy; runs `kfe2.py`.

## Third-party code and libraries

- `mechfight.py`: the stdin/stdout wrapper from the Builders mechfight kit (provided by the organizers), unchanged.
- [numpy](https://numpy.org) 2.1.3 (BSD-3-Clause), installed in the Docker image.
- Python 3.13 standard library.
