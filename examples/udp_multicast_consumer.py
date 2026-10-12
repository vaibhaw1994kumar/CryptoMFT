from __future__ import annotations

import argparse
import json
import socket
import struct


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Listen to crypto-stat-arb order-book, trade and OHLC multicast"
    )
    parser.add_argument("--group", default="239.255.77.77")
    parser.add_argument("--port", type=int, default=19001)
    parser.add_argument("--interface", default="0.0.0.0")
    parser.add_argument(
        "--type",
        choices=("ALL", "ORDERBOOK", "OHLC_1M", "TRADE", "LTP_OHLC_1M"),
        default="ALL",
    )
    args = parser.parse_args()

    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("", args.port))
    membership = socket.inet_aton(args.group) + socket.inet_aton(args.interface)
    listener.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)

    try:
        while True:
            data, source = listener.recvfrom(65_535)
            message = json.loads(data)
            if args.type != "ALL" and message.get("message_type") != args.type:
                continue
            print(json.dumps({"source": source, **message}, sort_keys=True), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()


if __name__ == "__main__":
    main()
