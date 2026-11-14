import logging
import threading

from waitress import serve

from .config import load_options
from .scheduler import Scheduler
from .web import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logging.getLogger("waitress").setLevel(logging.WARNING)


def main():
    opts = load_options()
    sched = Scheduler(opts["schedule"])
    threading.Thread(target=sched.loop, daemon=True, name="scheduler").start()
    threading.Thread(target=sched.listen_stdin, daemon=True, name="stdin").start()
    if opts["run_on_start"]:
        sched.trigger("startup")
    serve(create_app(sched), host="0.0.0.0", port=8099, threads=4)


if __name__ == "__main__":
    main()
