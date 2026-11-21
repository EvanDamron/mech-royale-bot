# Stage 1: build the vendored giddygiddyboy practice driver (Go, MIT; see vendor/giddy/LICENSE and README)
FROM golang:1.26.1-bookworm@sha256:ab3d6955bbc813a0f3fdf220c1d817dd89c0b3f283777db8ece4a32fe7858edd AS giddy
WORKDIR /src
COPY vendor/giddy/go.mod ./
COPY vendor/giddy/mech ./mech
COPY vendor/giddy/cmd ./cmd
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /giddy ./cmd/bot

# Final: our Python bot plus the driver binary
FROM python:3.13-slim
RUN pip install --no-cache-dir numpy==2.1.3
WORKDIR /bot
COPY mechfight.py worldmap.py bot.py hybrid.py ./
COPY vendor/giddy/LICENSE ./vendor/giddy/LICENSE
COPY --from=giddy /giddy ./vendor/giddy/giddy
ENTRYPOINT ["python3", "-u", "/bot/hybrid.py"]
