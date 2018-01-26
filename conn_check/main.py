from __future__ import print_function
from __future__ import unicode_literals
from future import standard_library
standard_library.install_aliases()
from builtins import str
from builtins import map
from builtins import object
from argparse import ArgumentParser
from collections import defaultdict
import sys
from threading import Thread
import time
import traceback
import yaml

from twisted.internet import reactor
from twisted.internet.defer import (
    inlineCallbacks,
    )
from twisted.python.threadpool import ThreadPool

from . import get_version_string
from .check_impl import (
    FailureCountingResultWrapper,
    parallel_check,
    skipping_check,
    ResultTracker,
    )
from .checks import CHECK_ALIASES, CHECKS, load_tls_certs
from .patterns import (
    SimplePattern,
    SumPattern,
    )


def setup_reactor(max_timeout):
    """Setup the Twisted reactor with required customisations."""

    def make_daemon_thread(*args, **kw):
        """Create a daemon thread."""
        thread = Thread(*args, **kw)
        thread.daemon = True
        return thread

    threadpool = ThreadPool(minthreads=1)
    threadpool.threadFactory = make_daemon_thread
    reactor.threadpool = threadpool
    reactor.callWhenRunning(threadpool.start)

    if max_timeout is not None:
        def terminator():
            # Hasta la vista, twisted
            reactor.stop()
            print('Maximum timeout reached: {}s'.format(max_timeout))

        reactor.callLater(max_timeout, terminator)
    return reactor


def check_from_description(check_description):
    _type = check_description['type']

    if _type in CHECK_ALIASES:
        _type = CHECK_ALIASES[_type]

    check = CHECKS.get(_type, None)
    if check is None:
        raise AssertionError("Unknown check type: {}, available checks: {}"
                             .format(_type, list(CHECKS.keys())))
    for arg in check['args']:
        if arg not in check_description:
            raise AssertionError('{} missing from check: {}'.format(arg,
                                 check_description))

    if 'port' in check_description:
        check_description['port'] = int(check_description['port'])

    res = check['fn'](**check_description)
    return res


def filter_tags(check, include, exclude):
    if not include and not exclude:
        return True

    check_tags = set(check.get('tags', []))

    if include:
        result = bool(check_tags.intersection(include))
    else:
        result = not bool(check_tags.intersection(exclude))

    return result


def build_checks(check_descriptions, connect_timeout, include_tags,
                 exclude_tags, skip_checks=False):
    def set_timeout(desc):
        new_desc = dict(timeout=connect_timeout)
        new_desc.update(desc)
        return new_desc
    check_descriptions = [c for c in check_descriptions if filter_tags(c, include_tags, exclude_tags)]

    subchecks = [check_from_description(c) for c in list(map(set_timeout, check_descriptions))]

    if skip_checks:
        strategy_wrapper = skipping_check
    else:
        strategy_wrapper = parallel_check
    return strategy_wrapper(subchecks)


@inlineCallbacks
def run_checks(checks, pattern, results):
    """Make and run all the pertinent checks."""
    try:
        yield checks.check(pattern, results)
    finally:
        reactor.stop()


class NagiosCompatibleArgsParser(ArgumentParser):

    def error(self, message):
        """A patched version of ArgumentParser.error.

        Does the same thing as ArgumentParser.error, e.g. prints an error
        message and exits, but does so with an exit code of 3 rather than 2,
        to maintain compatibility with Nagios checks.
        """
        self.print_usage(sys.stderr)
        self.exit(3, '{}: error: {}\n'.format(self.prog, message))


class TimestampOutput(object):

    def __init__(self, output):
        self.start = time.time()
        self.output = output

    def write(self, data):
        self.output.write("{:.3f}: {}".format(time.time() - self.start, data))


class OrderedOutput(object):
    """Outputs check results ordered by FAILED, SUCCESSFUL, SKIPPED checks."""

    def __init__(self, output):
        self.output = output

        self.failed = defaultdict(list)
        self.messages = defaultdict(list)
        self.skipped = []

    def write(self, data):
        if data[:7] == 'SKIPPED':
            self.skipped.append(data)
            return

        name, message = data.split(' ', 1)

        # Standard check name format is {type}:{host}:{port}
        name_parts = name.split(':', 2)
        try:
            name_parts[2] = ''
        except IndexError:
            pass
        name = ':'.join(name_parts)

        if message[0:6] == 'FAILED':
            self.failed[name].append(data)
        else:
            self.messages[name].append(data)

    def flush(self):
        for _type in ('failed', 'messages'):
            for name, messages in sorted(getattr(self, _type).items()):
                messages.sort()
                list(map(self.output.write, messages))

        self.skipped.sort()
        list(map(self.output.write, self.skipped))


class ConsoleOutput(ResultTracker):
    """Outputs check results to STDOUT."""

    def __init__(self, output, verbose, show_tracebacks, show_duration):
        """Initialize an instance."""
        super(ConsoleOutput, self).__init__()
        self.output = output
        self.verbose = verbose
        self.show_tracebacks = show_tracebacks
        self.show_duration = show_duration

    def format_duration(self, duration):
        if not self.show_duration:
            return ""
        return ": ({:.3f} ms)".format(duration)

    def notify_start(self, name, info):
        """Register the start of a check."""
        if self.verbose:
            if info:
                info = " ({})".format(info)
            else:
                info = ''
            self.output.write("Starting {}{}...\n".format(name, info))

    def notify_skip(self, name):
        """Register a check being skipped."""
        self.output.write("SKIPPED: {}\n".format(name))

    def notify_success(self, name, duration):
        """Register a success."""
        self.output.write("{} OK{}\n".format(
            name, self.format_duration(duration)))

    def notify_failure(self, name, info, exc_info, duration):
        """Register a failure."""
        message = str(exc_info[1]).split("\n")[0]
        if info:
            message = "({}) {}".format(info, message)
        self.output.write("{} FAILED{} - {}\n".format(
            name, self.format_duration(duration), message))

        if self.show_tracebacks:
            formatted = traceback.format_exception(exc_info[0],
                                                   exc_info[1],
                                                   exc_info[2],
                                                   None)
            lines = "".join(formatted).split("\n")
            if len(lines) > 0 and len(lines[-1]) == 0:
                lines.pop()
            indented = "\n".join(["  {}".format(line) for line in lines])
            self.output.write("{}\n".format(indented))


class Command(object):
    """CLI command runner for the main conn-check endpoint."""

    def __init__(self, args):
        self.make_arg_parser()
        self.parse_options(args)
        self.wrap_output(sys.stdout)
        self.load_descriptions()

    def make_arg_parser(self):
        """Set up an arg parser with our options."""

        parser = NagiosCompatibleArgsParser()
        parser.add_argument("config_file",
                            help="Config file specifying the checks to run.")
        parser.add_argument("patterns", nargs='*',
                            help="Patterns to filter the checks.")
        parser.add_argument("-v", "--verbose", dest="verbose",
                            action="store_true", default=False,
                            help="Show additional status")
        parser.add_argument("-d", "--duration", dest="show_duration",
                            action="store_true", default=False,
                            help="Show duration")
        parser.add_argument("-t", "--tracebacks", dest="show_tracebacks",
                            action="store_true", default=False,
                            help="Show tracebacks on failure")
        parser.add_argument("--validate", dest="validate",
                            action="store_true", default=False,
                            help="Only validate the config file,"
                            " don't run checks.")
        parser.add_argument("--version", dest="print_version",
                            action="store_true", default=False,
                            help="Print the currently installed version.")
        parser.add_argument("--tls-certs-path", dest="cacerts_path",
                            action="store", default="/etc/ssl/certs/",
                            help="Path to TLS CA certificates.")
        parser.add_argument("--max-timeout", dest="max_timeout", type=float,
                            action="store", help="Maximum execution time.")
        parser.add_argument("--connect-timeout", dest="connect_timeout",
                            action="store", default=10, type=float,
                            help="Network connection timeout.")
        parser.add_argument("-", "--unbuffered-output", dest="buffer_output",
                            action="store_false", default=True,
                            help="Don't buffer output, write to STDOUT right "
                            "away.")
        parser.add_argument("--dry-run",
                            dest="dry_run", action="store_true",
                            default=False,
                            help="Skip all checks, just print out"
                            " what would be run.")
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--include-tags", dest="include_tags",
                           action="store", default="",
                           help="Comma separated list of tags to include.")
        group.add_argument("--exclude-tags", dest="exclude_tags",
                           action="store", default="",
                           help="Comma separated list of tags to exclude.")
        self.parser = parser

    def parse_options(self, args):
        """Parse args (e.g. sys.argv) into options and set some config."""

        options = self.parser.parse_args(list(args))

        include_tags = []
        if options.include_tags:
            include_tags = options.include_tags.split(',')
            include_tags = [tag.strip() for tag in include_tags]
        options.include_tags = include_tags

        exclude_tags = []
        if options.exclude_tags:
            exclude_tags = options.exclude_tags.split(',')
            exclude_tags = [tag.strip() for tag in exclude_tags]
        options.exclude_tags = exclude_tags

        if options.patterns:
            self.patterns = SumPattern(list(map(SimplePattern, options.patterns)))
        else:
            self.patterns = SimplePattern("*")
        self.options = options

    def wrap_output(self, output):
        """Wraps an output stream (e.g. sys.stdout) from options."""

        if self.options.show_duration:
            output = TimestampOutput(output)
        if self.options.buffer_output:
            # We buffer output so we can order it for human readable output
            output = OrderedOutput(output)

        results = ConsoleOutput(output=output,
                                show_tracebacks=self.options.show_tracebacks,
                                show_duration=self.options.show_duration,
                                verbose=self.options.verbose)
        if not self.options.dry_run:
            results = FailureCountingResultWrapper(results)

        self.output = output
        self.results = results

    def load_descriptions(self):
        """Pre-load YAML checks file into a descriptions property."""

        with open(self.options.config_file) as f:
            self.descriptions = yaml.load(f)

    def run(self):
        """Run/validate/dry-run the given command with options."""

        checks = build_checks(self.descriptions,
                              self.options.connect_timeout,
                              self.options.include_tags,
                              self.options.exclude_tags,
                              self.options.dry_run)

        if not self.options.validate:
            if not self.options.dry_run:
                load_tls_certs(self.options.cacerts_path)

            setup_reactor(self.options.max_timeout)
            reactor.callWhenRunning(run_checks, checks, self.patterns,
                                    self.results)
            reactor.run()

            # Flush output, this really only has an effect when running
            # buffered output
            self.output.flush()

            if not self.options.dry_run and self.results.any_failed():
                return 2

        return 0


def parse_version_arg():
    """Manually check for --version in args and output version info.

    We need to do this early because ArgumentParser won't let us mix
    and match non-default positional argument with a flag argument.
    """
    if '--version' in sys.argv:
        sys.stdout.write('conn-check {}\n'.format(get_version_string()))
        return True


def run(*args):
    if parse_version_arg():
        return 0

    cmd = Command(args)
    return cmd.run()


def main():
    sys.exit(run(*sys.argv[1:]))


if __name__ == '__main__':
    main()
