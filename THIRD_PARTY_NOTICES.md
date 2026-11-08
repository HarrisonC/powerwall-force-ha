# Third-party notices

The RSA transport and battery-dispatch wire implementation was developed using
pyPowerwall as a reference. `tests/fixtures/upstream_combined.proto` is retained
from that project solely to verify compatibility with its generated protobuf
messages. Source: https://github.com/jasonacox/pypowerwall, retrieved 2026-11-05.

`custom_components/powerwall_force/status_query.py` retains the verbatim
`device_controller_basic` query and its signature from pyPowerwall's
`pypowerwall/tedapi/queries/V2024_06.json`, retrieved 2026-11-06. The query text
must remain byte-identical because it is signed by Tesla. The retained
`tests/fixtures/upstream_status_query.json` and `upstream_queries.proto` verify
the query and its wire encoding independently of the runtime implementation.
These materials are distributed under pyPowerwall's MIT license below.

MIT License

Copyright (c) 2021 Jason Cox

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

