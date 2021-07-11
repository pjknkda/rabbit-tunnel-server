import argparse

import uvicorn

try:
    import uvloop
    uvloop.install()
except ImportError:
    pass


def main() -> None:
    from . import Server

    arg_parser = argparse.ArgumentParser(description='A server for rabbit-tunnel')

    arg_parser.add_argument('-sd', '--service-domain', help='service domain', required=True)

    arg_parser.add_argument(
        '-wh',
        '--webserver-host',
        default='127.0.0.1',
        help='webserver host to bind (default: 127.0.0.1)',
    )
    arg_parser.add_argument(
        '-wp',
        '--webserver-port',
        default=43212,
        type=int,
        help='webserver port to bind (default: 43212)',
    )

    arg_parser.add_argument(
        '-mh',
        '--multiplexer-host',
        default='127.0.0.1',
        help='TCP multiplexer host to bind (default: 127.0.0.1)',
    )
    arg_parser.add_argument(
        '-mp',
        '--multiplexer-port',
        default=43213,
        type=int,
        help='TCP multiplexer port to bind (default: 43213)',
    )

    arg_parser.add_argument(
        '--debug',
        default=False,
        action='store_true',
        help='enable debug logs'
    )

    args = arg_parser.parse_args()

    try:
        server = Server(args.service_domain, args.multiplexer_host, args.multiplexer_port, args.debug)
        uvicorn.run(server.web_app, host=args.webserver_host, port=args.webserver_port, access_log=False)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
