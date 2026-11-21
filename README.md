# Kentucky Fried Evan: Mech Royale bot

A bot for the Builders Buildathon "Mech Royale" track (mechfight/1 protocol). Python 3.13 + numpy.

- `bot.py`: the bot (zone play, A* navigation, led gun fire, laser/lightning, cover retreats)
- `worldmap.py`: map model built from `/arena` at startup (terrain, building and bridge grids,
  line of sight, nav grid, A*)
- `mechfight.py`: the kit's protocol wrapper (unchanged)
- `sim/`: an approximate local re-implementation of the match server with house-bot imitations,
  for comparing bot versions. Not used at runtime.

## Local testing

```sh
python3 check/check.py -- python3 -u bot.py          # from the kit folder
python sim/run.py practice --bot "kfe=python -u bot.py" -n 16 -j 8
python sim/run.py ffa --bot "A=python -u bot.py" --bot "B=python -u other.py" -n 8 -j 4
```

`sim/` expects the kit at `../starter/mechfight-kit` (for the map and the recorded config).

## Third-party code (MIT)

`vendor/giddy/` is giddygiddyboy's Mech Royale bot (Go), used unchanged under its MIT License (`vendor/giddy/LICENSE`,
Copyright (c) 2026 giddygiddyboy). Source: `justnau/giddygiddyboy-mech-royale` at commit
`e19e3c8d45d980df35c2fa5318353aaeb7c515bd`, as bundled in `charliescull/mech-royale-bot` (`drivers/giddy/`).
The Dockerfile builds it in a pinned `golang:1.26.1-bookworm` stage. `Dockerfile.python` runs our own Python bot alone.
(Change on this branch: practice laser hold range 120 m -> 160 m in `squadParams`.)
