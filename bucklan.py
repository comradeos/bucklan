import argparse
from pathlib import Path
import sys

from app import messages
from app.client import download_file, ping, show_listing
from app.config import ENV_FILENAME, read_port
from app.files import check_root
from app.server import serve


ENV_FILE = Path(__file__).resolve().with_name(ENV_FILENAME)
CHECK_COMMAND = "check"
SERVE_COMMAND = "serve"
PING_COMMAND = "ping"
LIST_COMMAND = "list"
GET_COMMAND = "get"
IP_ARGUMENT = "ip"
PATH_ARGUMENT = "path"


# Зчитує аргументи, запускає вибрану команду й повертає код завершення.
def main() -> int:
    parser = argparse.ArgumentParser(description=messages.APP_DESCRIPTION)

    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(CHECK_COMMAND, help=messages.CHECK_HELP)
    commands.add_parser(SERVE_COMMAND, help=messages.SERVE_HELP)
    
    ping_parser = commands.add_parser(PING_COMMAND, help=messages.PING_HELP)
    ping_parser.add_argument(
        IP_ARGUMENT, metavar=messages.IP_ADDRESS_LABEL, help=messages.IP_ADDRESS_HELP
    )
    
    list_parser = commands.add_parser(LIST_COMMAND, help=messages.LIST_HELP)
    list_parser.add_argument(
        IP_ARGUMENT, metavar=messages.IP_ADDRESS_LABEL, help=messages.IP_ADDRESS_HELP
    )
    
    get_parser = commands.add_parser(GET_COMMAND, help=messages.GET_HELP)
    get_parser.add_argument(
        IP_ARGUMENT, metavar=messages.IP_ADDRESS_LABEL, help=messages.IP_ADDRESS_HELP
    )
    get_parser.add_argument(
        PATH_ARGUMENT, metavar=messages.FILE_PATH_LABEL, help=messages.FILE_PATH_HELP
    )

    args = parser.parse_args()

    try:
        if args.command == CHECK_COMMAND:
            print(messages.ROOT_PATH.format(path=check_root(ENV_FILE)))
        elif args.command == SERVE_COMMAND:
            serve(read_port(ENV_FILE), ENV_FILE)
        elif args.command == PING_COMMAND:
            ping(args.ip, read_port(ENV_FILE))
        elif args.command == LIST_COMMAND:
            show_listing(args.ip, read_port(ENV_FILE), ENV_FILE)
        else:
            download_file(args.ip, read_port(ENV_FILE), args.path, ENV_FILE)
    except ValueError as error:
        print(messages.COMMAND_ERROR.format(error=error), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
