# -*- test-case-name: vumi.application.tests.test_sandbox -*-

"""An application for sandboxing message processing."""

import resource
import os
import json
import pkg_resources
import logging
from uuid import uuid4

from twisted.internet import reactor
from twisted.internet.protocol import ProcessProtocol
from twisted.internet.defer import (
    Deferred, inlineCallbacks, maybeDeferred, returnValue, DeferredList,
    succeed)
from twisted.internet.error import ProcessDone
from twisted.python.failure import Failure

from vumi.config import ConfigText, ConfigInt, ConfigList, ConfigDict
from vumi.application.base import ApplicationWorker
from vumi.message import Message
from vumi.errors import ConfigError
from vumi.persist.txredis_manager import TxRedisManager
from vumi.utils import load_class_by_string, http_request_full
from vumi import log
from vumi.application.sandbox_rlimiter import SandboxRlimiter


class MultiDeferred(object):
    """A callable that returns new deferreds each time and
    then fires them all together."""

    NOT_FIRED = object()

    def __init__(self):
        self._result = self.NOT_FIRED
        self._deferreds = []

    def callback(self, result):
        self._result = result
        for d in self._deferreds:
            d.callback(result)
        self._deferreds = []

    def get(self):
        d = Deferred()
        if self.fired():
            d.callback(self._result)
        else:
            self._deferreds.append(d)
        return d

    def fired(self):
        return self._result is not self.NOT_FIRED


class SandboxError(Exception):
    """An error occurred inside the sandbox."""


class SandboxProtocol(ProcessProtocol):
    """A protocol for communicating over stdin and stdout with a sandboxed
    process.

    The sandbox process is created by calling :meth:`spawn`. This:

    * Spawns a new Python process that applies the supplied rlimits.
    * The spawned process then `execs` the supplied executable.

    Once a spawned process starts, the parent process communicates with
    it over `stdin`, `stdout` and `stderr` reading and writing a stream
    of newline separated JSON commands that are parsed and formatted by
    :class:`SandboxCommand`.

    Incoming commands are dispatched to :class:`SandboxResource` instances
    via the supplied :class:`SandboxApi`.
    """

    def __init__(self, sandbox_id, api, executable, spawn_kwargs,
                 rlimits, timeout, recv_limit):
        self.sandbox_id = sandbox_id
        self.api = api
        self.executable = executable
        self.spawn_kwargs = spawn_kwargs
        self.rlimits = rlimits
        self._started = MultiDeferred()
        self._done = MultiDeferred()
        self._pending_requests = []
        self.exit_reason = None
        self.timeout_task = reactor.callLater(timeout, self.kill)
        self.recv_limit = recv_limit
        self.recv_bytes = 0
        self.chunk = ''
        self.error_chunk = ''
        api.set_sandbox(self)

    def spawn(self):
        SandboxRlimiter.spawn(
            reactor, self, self.executable, self.rlimits, **self.spawn_kwargs)

    def done(self):
        """Returns a deferred that will be called when the process ends."""
        return self._done.get()

    def started(self):
        """Returns a deferred that will be called once the process starts."""
        return self._started.get()

    def kill(self):
        """Kills the underlying process."""
        if self.transport.pid is not None:
            self.transport.signalProcess('KILL')

    def send(self, command):
        """Writes the command to the processes' stdin."""
        self.transport.write(command.to_json())
        self.transport.write("\n")

    def check_recv(self, nbytes):
        self.recv_bytes += nbytes
        if self.recv_bytes <= self.recv_limit:
            return True
        else:
            self.kill()
            self.api.log("Sandbox %r killed for producing too much data on"
                         " stderr and stdout." % (self.sandbox_id),
                         level=logging.ERROR)
            return False

    def connectionMade(self):
        self._started.callback(self)

    def _process_data(self, chunk, data):
        if not self.check_recv(len(data)):
            return ['']  # skip the data if it's too big
        line_parts = data.split("\n")
        line_parts[0] = chunk + line_parts[0]
        return line_parts

    def _parse_command(self, line):
        try:
            return SandboxCommand.from_json(line)
        except Exception, e:
            return SandboxCommand(cmd="unknown", line=line, exception=e)

    def outReceived(self, data):
        lines = self._process_data(self.chunk, data)
        for i in range(len(lines) - 1):
            d = self.api.dispatch_request(self._parse_command(lines[i]))
            self._pending_requests.append(d)
        self.chunk = lines[-1]

    def outConnectionLost(self):
        if self.chunk:
            line, self.chunk = self.chunk, ""
            d = self.api.dispatch_request(self._parse_command(line))
            self._pending_requests.append(d)

    def errReceived(self, data):
        lines = self._process_data(self.error_chunk, data)
        for i in range(len(lines) - 1):
            self.api.log(lines[i], logging.ERROR)
        self.error_chunk = lines[-1]

    def errConnectionLost(self):
        if self.error_chunk:
            self.api.log(self.error_chunk, logging.ERROR)
            self.error_chunk = ""

    def _process_request_results(self, results):
        for success, result in results:
            if not success:
                # errors here are bugs in Vumi and thus should always
                # be logged via Twisted too.
                log.error(result)
                # we log them again in a simplified form via the sandbox
                # api so that the sandbox owner gets to see them too
                self.api.log(result.getErrorMessage(), logging.ERROR)

    def processEnded(self, reason):
        if self.timeout_task.active():
            self.timeout_task.cancel()
        if isinstance(reason.value, ProcessDone):
            result = reason.value.status
        else:
            result = reason
        if not self._started.fired():
            self._started.callback(Failure(
                SandboxError("Process failed to start.")))
        requests_done = DeferredList(self._pending_requests)
        requests_done.addCallback(self._process_request_results)
        requests_done.addCallback(lambda _r: self._done.callback(result))


class SandboxResources(object):
    """Class for holding resources common to a set of sandboxes."""

    def __init__(self, app_worker, config):
        self.app_worker = app_worker
        self.config = config
        self.resources = {}

    def add_resource(self, resource_name, resource):
        """Add additional resources -- should only be called before
           calling :meth:`setup_resources`."""
        self.resources[resource_name] = resource

    def validate_config(self):
        # FIXME: The name of this method is a vicious lie.
        #        It does not validate configs. It constructs resources objects.
        #        Fixing that is beyond the scope of this commit, however.
        for name, config in self.config.iteritems():
            cls = load_class_by_string(config.pop('cls'))
            self.resources[name] = cls(name, self.app_worker, config)

    @inlineCallbacks
    def setup_resources(self):
        for resource in self.resources.itervalues():
            yield resource.setup()

    @inlineCallbacks
    def teardown_resources(self):
        for resource in self.resources.itervalues():
            yield resource.teardown()


class SandboxResource(object):
    """Base class for sandbox resources."""
    # TODO: SandboxResources should probably have their own config definitions.
    #       Is that overkill?

    def __init__(self, name, app_worker, config):
        self.name = name
        self.app_worker = app_worker
        self.config = config

    def setup(self):
        pass

    def teardown(self):
        pass

    def sandbox_init(self, api):
        pass

    def reply(self, command, **kwargs):
        return SandboxCommand(cmd=command['cmd'], reply=True,
                              cmd_id=command['cmd_id'], **kwargs)

    def dispatch_request(self, api, command):
        handler_name = 'handle_%s' % (command['cmd'],)
        handler = getattr(self, handler_name, self.unknown_request)
        return maybeDeferred(handler, api, command)

    def unknown_request(self, api, command):
        api.log("Resource %s received unknown command %r from"
                " sandbox %r. Killing sandbox. [Full command: %r]"
                % (self.name, command['cmd'], api.sandbox_id, command),
                logging.ERROR)
        api.sandbox_kill()  # it's a harsh world


class RedisResource(SandboxResource):
    """Resource that provices access to a simple key-value store.

    Configuration options:

    :param dict redis_manager:
        Redis manager configuration options.
    :param int keys_per_user:
        Maximum number of keys each user may make use of in redis
        (default: 100).
    """

    @inlineCallbacks
    def setup(self):
        self.r_config = self.config.get('redis_manager', {})
        self.keys_per_user = self.config.get('keys_per_user', 100)
        self.redis = yield TxRedisManager.from_config(self.r_config)

    def teardown(self):
        return self.redis.close_manager()

    def _count_key(self, sandbox_id):
        return "#".join(["count", sandbox_id])

    def _sandboxed_key(self, sandbox_id, key):
        return "#".join(["sandboxes", sandbox_id, key])

    def _too_many_keys(self, command):
        return self.reply(command, success=False,
                          reason="Too many keys")

    @inlineCallbacks
    def check_keys(self, sandbox_id, key):
        if (yield self.redis.exists(key)):
            returnValue(True)
        count_key = self._count_key(sandbox_id)
        if (yield self.redis.incr(count_key, 1)) > self.keys_per_user:
            yield self.redis.incr(count_key, -1)
            returnValue(False)
        returnValue(True)

    @inlineCallbacks
    def handle_set(self, api, command):
        key = self._sandboxed_key(api.sandbox_id, command.get('key'))
        if not (yield self.check_keys(api.sandbox_id, key)):
            returnValue(self._too_many_keys(command))
        value = command.get('value')
        yield self.redis.set(key, json.dumps(value))
        returnValue(self.reply(command, success=True))

    @inlineCallbacks
    def handle_get(self, api, command):
        key = self._sandboxed_key(api.sandbox_id, command.get('key'))
        raw_value = yield self.redis.get(key)
        value = json.loads(raw_value) if raw_value is not None else None
        returnValue(self.reply(command, success=True,
                               value=value))

    @inlineCallbacks
    def handle_delete(self, api, command):
        key = self._sandboxed_key(api.sandbox_id, command.get('key'))
        existed = bool((yield self.redis.delete(key)))
        if existed:
            count_key = self._count_key(api.sandbox_id)
            yield self.redis.incr(count_key, -1)
        returnValue(self.reply(command, success=True,
                               existed=existed))

    @inlineCallbacks
    def handle_incr(self, api, command):
        key = self._sandboxed_key(api.sandbox_id, command.get('key'))
        if not (yield self.check_keys(api.sandbox_id, key)):
            returnValue(self._too_many_keys(command))
        amount = command.get('amount', 1)
        try:
            value = yield self.redis.incr(key, amount=amount)
        except Exception, e:
            returnValue(self.reply(command, success=False, reason=unicode(e)))
        returnValue(self.reply(command, value=int(value), success=True))


class OutboundResource(SandboxResource):
    """Resource that provides the ability to send outbound messages.
    """

    def handle_reply_to(self, api, command):
        content = command['content']
        continue_session = command.get('continue_session', True)
        orig_msg = api.get_inbound_message(command['in_reply_to'])
        self.app_worker.reply_to(orig_msg, content,
                                 continue_session=continue_session)

    def handle_reply_to_group(self, api, command):
        content = command['content']
        continue_session = command.get('continue_session', True)
        orig_msg = api.get_inbound_message(command['in_reply_to'])
        self.app_worker.reply_to_group(orig_msg, content,
                                       continue_session=continue_session)

    def handle_send_to(self, api, command):
        content = command['content']
        to_addr = command['to_addr']
        endpoint = command.get('endpoint', 'default')
        self.app_worker.send_to(to_addr, content, endpoint=endpoint)


class JsSandboxResource(SandboxResource):
    """Resource that initializes a Javascript sandbox.

    Typically used alongside vumi/applicaiton/sandboxer.js which is
    a simple node.js based Javascript sandbox.

    Requires the worker to have a `javascript_for_api` method.
    """
    def sandbox_init(self, api):
        javascript = self.app_worker.javascript_for_api(api)
        app_context = self.app_worker.app_context_for_api(api)
        api.sandbox_send(SandboxCommand(cmd="initialize",
                                        javascript=javascript,
                                        app_context=app_context))


class LoggingResource(SandboxResource):
    """Resource that allows a sandbox to log messages via Twisted's
    logging framework.
    """
    def log(self, api, msg, level):
        """Logs a message via vumi.log (i.e. Twisted logging).

        Sub-class should override this if they wish to log messages
        elsewhere. The `api` parameter is provided for use by such
        sub-classes.

        The `log` method should always return a deferred.
        """
        return succeed(log.msg(msg, logLevel=level))

    @inlineCallbacks
    def handle_log(self, api, command, level=None):
        level = command.get('level', level)
        if level is None:
            level = logging.INFO
        msg = command.get('msg')
        if msg is None:
            returnValue(self.reply(command, success=False,
                                   reason="Value expected for msg"))
        msg = str(msg)
        yield self.log(api, msg, level)
        returnValue(self.reply(command, success=True))

    def handle_debug(self, api, command):
        return self.handle_log(api, command, level=logging.DEBUG)

    def handle_info(self, api, command):
        return self.handle_log(api, command, level=logging.INFO)

    def handle_warning(self, api, command):
        return self.handle_log(api, command, level=logging.WARNING)

    def handle_error(self, api, command):
        return self.handle_log(api, command, level=logging.ERROR)

    def handle_critical(self, api, command):
        return self.handle_log(api, command, level=logging.CRITICAL)


class HttpClientResource(SandboxResource):
    """Resource that allows making HTTP calls to outside services."""

    DEFAULT_TIMEOUT = 30  # seconds
    DEFAULT_DATA_LIMIT = 128 * 1024  # 128 KB

    def setup(self):
        self.timeout = self.config.get('timeout', self.DEFAULT_TIMEOUT)
        self.data_limit = self.config.get('data_limit',
                                          self.DEFAULT_DATA_LIMIT)

    def _make_request_from_command(self, method, command):
        url = command.get('url', None)
        if not isinstance(url, basestring):
            return succeed(self.reply(command, success=False,
                                      reason="No URL given"))
        url = url.encode("utf-8")
        headers = command.get('headers', {})
        headers = dict((k.encode("utf-8"), [x.encode("utf-8") for x in v])
                       for k, v in headers.items())
        data = command.get('data', None)
        if data is not None:
            data = data.encode("utf-8")
        d = http_request_full(url, data=data, headers=headers,
                              method=method, timeout=self.timeout,
                              data_limit=self.data_limit)
        d.addCallback(self._make_success_reply, command)
        d.addErrback(self._make_failure_reply, command)
        return d

    def _make_success_reply(self, response, command):
        return self.reply(command, success=True,
                          body=response.delivered_body,
                          code=response.code)

    def _make_failure_reply(self, failure, command):
        return self.reply(command, success=False,
                          reason=failure.getErrorMessage())

    def handle_get(self, api, command):
        return self._make_request_from_command('GET', command)

    def handle_put(self, api, command):
        return self._make_request_from_command('PUT', command)

    def handle_delete(self, api, command):
        return self._make_request_from_command('DELETE', command)

    def handle_head(self, api, command):
        return self._make_request_from_command('HEAD', command)

    def handle_post(self, api, command):
        return self._make_request_from_command('POST', command)


class SandboxApi(object):
    """A sandbox API instance for a particular sandbox run."""

    def __init__(self, resources, config):
        self._sandbox = None
        self._inbound_messages = {}
        self.resources = resources
        self.fallback_resource = SandboxResource("fallback", None, {})
        potential_logger = None
        if config.logging_resource:
            potential_logger = self.resources.resources.get(
                config.logging_resource)
            if potential_logger is None:
                log.warning("Failed to find logging resource %r."
                            " Falling back to Twisted logging."
                            % (config.logging_resource,))
            elif not hasattr(potential_logger, 'log'):
                log.warning("Logging resource %r has no attribute 'log'."
                            " Falling abck to Twisted logging."
                            % (config.logging_resource,))
                potential_logger = None
        self.logging_resource = potential_logger
        self.config = config

    @property
    def sandbox_id(self):
        return self._sandbox.sandbox_id

    def set_sandbox(self, sandbox):
        if self._sandbox is not None:
            raise SandboxError("Sandbox already set ("
                               "existing id: %r, new id: %r)."
                               % (self.sandbox_id, sandbox.sandbox_id))
        self._sandbox = sandbox

    def sandbox_init(self):
        for resource in self.resources.resources.values():
            resource.sandbox_init(self)

    def sandbox_inbound_message(self, msg):
        self._inbound_messages[msg['message_id']] = msg
        self.sandbox_send(SandboxCommand(cmd="inbound-message",
                                         msg=msg.payload))

    def sandbox_inbound_event(self, event):
        self.sandbox_send(SandboxCommand(cmd="inbound-event",
                                         msg=event.payload))

    def sandbox_send(self, msg):
        self._sandbox.send(msg)

    def sandbox_kill(self):
        self._sandbox.kill()

    def get_inbound_message(self, message_id):
        return self._inbound_messages.get(message_id)

    def log(self, msg, level):
        if self.logging_resource is None:
            # fallback to vumi.log logging if we don't
            # have a logging resource.
            return succeed(log.msg(msg, logLevel=level))
        else:
            return self.logging_resource.log(self, msg, level=level)

    @inlineCallbacks
    def dispatch_request(self, command):
        resource_name, sep, rest = command['cmd'].partition('.')
        if not sep:
            resource_name, rest = '', resource_name
        command['cmd'] = rest
        resource = self.resources.resources.get(resource_name,
                                                self.fallback_resource)
        try:
            reply = yield resource.dispatch_request(self, command)
        except Exception, e:
            # errors here are bugs in Vumi so we always log them
            # via Twisted. However, we reply to the sandbox with
            # a failure and log via the sandbox api so that the
            # sandbox owner can be notified.
            log.error()
            self.log(str(e), level=logging.ERROR)
            reply = SandboxCommand(
                reply=True,
                cmd_id=command['cmd_id'],
                success=False,
                reason=unicode(e))

        if reply is not None:
            reply['cmd'] = '%s%s%s' % (resource_name, sep, rest)
            self.sandbox_send(reply)


class SandboxCommand(Message):
    @staticmethod
    def generate_id():
        return uuid4().get_hex()

    def process_fields(self, fields):
        fields = super(SandboxCommand, self).process_fields(fields)
        fields.setdefault('cmd', 'unknown')
        fields.setdefault('cmd_id', self.generate_id())
        fields.setdefault('reply', False)
        return fields

    def validate_fields(self):
        super(SandboxCommand, self).validate_fields()
        self.assert_field_present(
            'cmd',
            'cmd_id',
            'reply',
        )


class SandboxConfig(ApplicationWorker.CONFIG_CLASS):

    sandbox = ConfigDict(
        "Dictionary of resources to provide to the sandbox."
        " Keys are the names of resources (as seen inside the sandbox)."
        " Values are dictionaries which must contain a `cls` key that"
        " gives the full name of the class that provides the resource."
        " Other keys are additional configuration for that resource.",
        default={}, static=True)

    executable = ConfigText(
        "Full path to the executable to run in the sandbox.")
    args = ConfigList(
        "List of arguments to pass to the executable (not including"
        " the path of the executable itself).", default=[])
    path = ConfigText("Current working directory to run the executable in.")
    env = ConfigDict(
        "Custom environment variables for the sandboxed process.", default={})
    timeout = ConfigInt(
        "Length of time the subprocess is given to process a message.",
        default=60)
    recv_limit = ConfigInt(
        "Maximum number of bytes that will be read from a sandboxed"
        " process' stdout and stderr combined.", default=1024 * 1024)
    rlimits = ConfigDict(
        "Dictionary of resource limits to be applied to sandboxed"
        " processes. Defaults are fairly restricted. Keys maybe"
        " names or values of the RLIMIT constants in"
        " Python `resource` module. Values should be appropriate integers.",
        default={})
    logging_resource = ConfigText(
        "Name of the logging resource to use to report errors detected"
        " in sandboxed code (e.g. lines written to stderr, unexpected"
        " process termination). Set to null to disable and report"
        " these directly using Twisted logging instead.",
        default=None)
    sandbox_id = ConfigText("This is set based on individual messages.")


class Sandbox(ApplicationWorker):
    """Sandbox application worker."""

    CONFIG_CLASS = SandboxConfig

    KB, MB = 1024, 1024 * 1024
    DEFAULT_RLIMITS = {
        resource.RLIMIT_CORE: (1 * MB, 1 * MB),
        resource.RLIMIT_CPU: (60, 60),
        resource.RLIMIT_FSIZE: (1 * MB, 1 * MB),
        resource.RLIMIT_DATA: (32 * MB, 32 * MB),
        resource.RLIMIT_STACK: (1 * MB, 1 * MB),
        resource.RLIMIT_RSS: (10 * MB, 10 * MB),
        resource.RLIMIT_NOFILE: (15, 15),
        resource.RLIMIT_MEMLOCK: (64 * KB, 64 * KB),
        resource.RLIMIT_AS: (196 * MB, 196 * MB),
    }

    def validate_config(self):
        config = self.get_static_config()
        self.resources = self.create_sandbox_resources(config.sandbox)
        self.resources.validate_config()

    def get_config(self, msg):
        config = self.config.copy()
        config['sandbox_id'] = self.sandbox_id_for_message(msg)
        return succeed(self.CONFIG_CLASS(config))

    def _convert_rlimits(self, rlimits_config):
        rlimits = dict((getattr(resource, key, key), value) for key, value in
                       rlimits_config.iteritems())
        for key in rlimits.iterkeys():
            if not isinstance(key, (int, long)):
                raise ConfigError("Unknown resource limit key %r" % (key,))
        return rlimits

    def setup_application(self):
        return self.resources.setup_resources()

    def teardown_application(self):
        return self.resources.teardown_resources()

    def setup_connectors(self):
        # Set the default event handler so we can handle events from any
        # endpoint.
        d = super(Sandbox, self).setup_connectors()

        def cb(connector):
            connector.set_default_event_handler(self.dispatch_event)
            return connector

        return d.addCallback(cb)

    def create_sandbox_resources(self, config):
        return SandboxResources(self, config)

    def get_executable_and_args(self, config):
        return config.executable, [config.executable] + config.args

    def get_rlimits(self, config):
        rlimits = self.DEFAULT_RLIMITS.copy()
        rlimits.update(self._convert_rlimits(config.rlimits))
        return rlimits

    def create_sandbox_protocol(self, api):
        executable, args = self.get_executable_and_args(api.config)
        rlimits = self.get_rlimits(api.config)
        spawn_kwargs = dict(
            args=args, env=api.config.env, path=api.config.path)
        return SandboxProtocol(
            api.config.sandbox_id, api, executable, spawn_kwargs, rlimits,
            api.config.timeout, api.config.recv_limit)

    def create_sandbox_api(self, resources, config):
        return SandboxApi(resources, config)

    def sandbox_id_for_message(self, msg_or_event):
        """Return a sandbox id for a message or event.

        Sub-classes may override this to retrieve an appropriate id.
        """
        return msg_or_event['sandbox_id']

    def sandbox_protocol_for_message(self, msg_or_event, config):
        """Return a sandbox protocol for a message or event.

        Sub-classes may override this to retrieve an appropriate protocol.
        """
        api = self.create_sandbox_api(self.resources, config)
        protocol = self.create_sandbox_protocol(api)
        return protocol

    def _process_in_sandbox(self, sandbox_protocol, api_callback):
        sandbox_protocol.spawn()

        def on_start(_result):
            sandbox_protocol.api.sandbox_init()
            api_callback()
            d = sandbox_protocol.done()
            d.addErrback(log.error)
            return d

        d = sandbox_protocol.started()
        d.addCallbacks(on_start, log.error)
        return d

    @inlineCallbacks
    def process_message_in_sandbox(self, msg):
        config = yield self.get_config(msg)
        sandbox_protocol = yield self.sandbox_protocol_for_message(msg, config)

        def sandbox_init():
            sandbox_protocol.api.sandbox_inbound_message(msg)

        status = yield self._process_in_sandbox(sandbox_protocol, sandbox_init)
        returnValue(status)

    @inlineCallbacks
    def process_event_in_sandbox(self, event):
        config = yield self.get_config(event)
        sandbox_protocol = yield self.sandbox_protocol_for_message(
            event, config)

        def sandbox_init():
            sandbox_protocol.api.sandbox_inbound_event(event)

        status = yield self._process_in_sandbox(sandbox_protocol, sandbox_init)
        returnValue(status)

    def consume_user_message(self, msg):
        return self.process_message_in_sandbox(msg)

    def close_session(self, msg):
        return self.process_message_in_sandbox(msg)

    def consume_ack(self, event):
        return self.process_event_in_sandbox(event)

    def consume_nack(self, event):
        return self.process_event_in_sandbox(event)

    def consume_delivery_report(self, event):
        return self.process_event_in_sandbox(event)


class JsSandboxConfig(SandboxConfig):
    "JavaScript sandbox configuration."

    javascript = ConfigText("JavaScript code to run.", required=True)
    app_context = ConfigText("Custom context to execute JS with.")
    logging_resource = ConfigText(
        "Name of the logging resource to use to report errors detected"
        " in sandboxed code (e.g. lines written to stderr, unexpected"
        " process termination). Set to null to disable and report"
        " these directly using Twisted logging instead.",
        default='log')


class JsSandbox(Sandbox):
    """
    Configuration options:

    As for :class:`Sandbox` except:

    * `executable` defaults to searching for a `node.js` binary.
    * `args` defaults to the JS sandbox script in the `vumi.application`
      module.
    * An instance of :class:`JsSandboxResource` is added to the sandbox
      resources under the name `js` if no `js` resource exists.
    * An instance of :class:`LoggingResource` is added to the sandbox
      resources under the name `log` if no `log` resource exists.
    * `logging_resource` is set to `log` if it is not set.
    * An extra 'javascript' parameter specifies the javascript to execute.
    * An extra optional 'app_context' parameter specifying a custom
      context for the 'javascript' application to execute with.

    Example 'javascript' that logs information via the sandbox API
    (provided as 'this' to 'on_inbound_message') and checks that logging
    was successful::

        api.on_inbound_message = function(command) {
            this.log_info("From command: inbound-message", function (reply) {
                this.log_info("Log successful: " + reply.success);
                this.done();
            });
        }

    Example 'app_context' that makes the Node.js 'path' module
    available under the name 'path' in the context that the sandboxed
    javascript executes in::

        {path: require('path')}
    """

    CONFIG_CLASS = JsSandboxConfig

    POSSIBLE_NODEJS_EXECUTABLES = [
        '/usr/local/bin/node',
        '/usr/local/bin/nodejs',
        '/usr/bin/node',
        '/usr/bin/nodejs',
    ]

    @classmethod
    def find_nodejs(cls):
        for path in cls.POSSIBLE_NODEJS_EXECUTABLES:
            if os.path.isfile(path):
                return path
        return None

    @classmethod
    def find_sandbox_js(cls):
        return pkg_resources.resource_filename('vumi.application',
                                               'sandboxer.js')

    def get_js_resource(self):
        return JsSandboxResource('js', self, {})

    def get_log_resource(self):
        return LoggingResource('log', self, {})

    def javascript_for_api(self, api):
        """Called by JsSandboxResource.

        :returns: String containing Javascript for the app to run.
        """
        return api.config.javascript

    def app_context_for_api(self, api):
        """Called by JsSandboxResource

        :returns: String containing Javascript expression that returns
        addition context for the namespace the app is being run
        in. This Javascript is expected to be trusted code.
        """
        return api.config.app_context

    def get_executable_and_args(self, config):
        executable = config.executable
        if executable is None:
            executable = self.find_nodejs()

        args = [executable] + (config.args or [self.find_sandbox_js()])

        return executable, args

    def validate_config(self):
        super(JsSandbox, self).validate_config()
        if 'js' not in self.resources.resources:
            self.resources.add_resource('js', self.get_js_resource())
        if 'log' not in self.resources.resources:
            self.resources.add_resource('log', self.get_log_resource())


class JsFileSandbox(JsSandbox):

    class CONFIG_CLASS(SandboxConfig):
        javascript_file = ConfigText(
            "The file containting the Javascript to run", required=True)
        app_context = ConfigText("Custom context to execute JS with.")

    def javascript_for_api(self, api):
        return file(api.config.javascript_file).read()
