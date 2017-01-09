#  Copyright 2008-2015 Nokia Solutions and Networks
#  Copyright 2016- Robot Framework Foundation
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

from __future__ import print_function

from collections import Mapping
import errno
import inspect
import re
import signal
import select
import sys
import traceback

if sys.version_info < (3,):
    from StringIO import StringIO
    from SimpleXMLRPCServer import SimpleXMLRPCServer
    from xmlrpclib import Binary, ServerProxy
    PY2, PY3 = True, False
else:
    from io import StringIO
    from xmlrpc.client import Binary, ServerProxy
    from xmlrpc.server import SimpleXMLRPCServer
    PY2, PY3 = False, True
    unicode = str
    long = int


__version__ = 'devel'

BINARY = re.compile('[\x00-\x08\x0B\x0C\x0E-\x1F]')
NON_ASCII = re.compile('[\x80-\xff]')


class RobotRemoteServer(SimpleXMLRPCServer):
    allow_reuse_address = True

    def __init__(self, library, host='127.0.0.1', port=8270, port_file=None,
                 allow_stop=True):
        """Configure and start-up remote server.

        :param library:     Test library instance or module to host.
        :param host:        Address to listen. Use ``'0.0.0.0'`` to listen
                            to all available interfaces.
        :param port:        Port to listen. Use ``0`` to select a free port
                            automatically. Can be given as an integer or as
                            a string.
        :param port_file:   File to write port that is used. ``None`` means
                            no such file is written.
        :param allow_stop:  Allow/disallow stopping the server using
                            ``Stop Remote Server`` keyword.
        """
        SimpleXMLRPCServer.__init__(self, (host, int(port)), logRequests=False)
        self._library = RemoteLibrary(library, self.stop_remote_server)
        self._allow_stop = allow_stop
        self._shutdown = False
        self._register_functions()
        self._register_signal_handlers()
        self._announce_start(port_file)
        self.serve_forever()

    def _register_functions(self):
        self.register_function(self.get_keyword_names)
        self.register_function(self.run_keyword)
        self.register_function(self.get_keyword_arguments)
        self.register_function(self.get_keyword_documentation)
        self.register_function(self.stop_remote_server)

    def _register_signal_handlers(self):
        def stop_with_signal(signum, frame):
            self._allow_stop = True
            self.stop_remote_server()
        for name in 'SIGINT', 'SIGTERM', 'SIGHUP':
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), stop_with_signal)

    def _announce_start(self, port_file=None):
        host, port = self.server_address
        self._log('Robot Framework remote server at %s:%s starting.'
                  % (host, port))
        if port_file:
            with open(port_file, 'w') as pf:
                pf.write(str(port))

    def serve_forever(self):
        if hasattr(self, 'timeout'):
            self.timeout = 0.5
        elif sys.platform.startswith('java'):
            self.socket.settimeout(0.5)
        while not self._shutdown:
            try:
                self.handle_request()
            except (OSError, select.error) as err:
                if err.args[0] != errno.EINTR:
                    raise

    def stop_remote_server(self):
        prefix = 'Robot Framework remote server at %s:%s ' % self.server_address
        if self._allow_stop:
            self._log(prefix + 'stopping.')
            self._shutdown = True
        else:
            self._log(prefix + 'does not allow stopping.', 'WARN')
        return self._shutdown

    def _log(self, msg, level=None):
        if level:
            msg = '*%s* %s' % (level.upper(), msg)
        self._write_to_stream(msg, sys.stdout)
        if sys.__stdout__ is not sys.stdout:
            self._write_to_stream(msg, sys.__stdout__)

    def _write_to_stream(self, msg, stream):
        stream.write(msg + '\n')
        stream.flush()

    def get_keyword_names(self):
        return self._library.get_keyword_names()

    def run_keyword(self, name, args, kwargs=None):
        return self._library.run_keyword(name, args, kwargs)

    def get_keyword_arguments(self, name):
        return self._library.get_keyword_arguments(name)

    def get_keyword_documentation(self, name):
        return self._library.get_keyword_documentation(name)


class RemoteLibrary(object):

    def __init__(self, library, stop_remote_server=None):
        self._library = library
        self._stop_remote_server = stop_remote_server

    def get_keyword_names(self):
        get_kw_names = (getattr(self._library, 'get_keyword_names', None) or
                        getattr(self._library, 'getKeywordNames', None))
        if self._is_function_or_method(get_kw_names):
            names = get_kw_names()
        else:
            names = [attr for attr in dir(self._library) if attr[0] != '_' and
                     self._is_function_or_method(getattr(self._library, attr))]
        if self._stop_remote_server:
            names.append('stop_remote_server')
        return names

    def _is_function_or_method(self, item):
        return inspect.isfunction(item) or inspect.ismethod(item)

    def run_keyword(self, name, args, kwargs=None):
        kw = self._get_keyword(name)
        return KeywordRunner(kw).run_keyword(args, kwargs)

    def _get_keyword(self, name):
        if name == 'stop_remote_server':
            return self._stop_remote_server
        kw = getattr(self._library, name, None)
        if not self._is_function_or_method(kw):
            return None
        return kw

    def get_keyword_arguments(self, name):
        kw = self._get_keyword(name)
        if not kw:
            return []
        return self._arguments_from_kw(kw)

    def _arguments_from_kw(self, kw):
        args, varargs, kwargs, defaults = inspect.getargspec(kw)
        if inspect.ismethod(kw):
            args = args[1:]  # drop 'self'
        if defaults:
            args, names = args[:-len(defaults)], args[-len(defaults):]
            args += ['%s=%s' % (n, d) for n, d in zip(names, defaults)]
        if varargs:
            args.append('*%s' % varargs)
        if kwargs:
            args.append('**%s' % kwargs)
        return args

    def get_keyword_documentation(self, name):
        if name == '__intro__':
            return inspect.getdoc(self._library) or ''
        if name == '__init__' and inspect.ismodule(self._library):
            return ''
        return inspect.getdoc(self._get_keyword(name)) or ''


class KeywordRunner(object):

    def __init__(self, keyword):
        self._keyword = keyword

    def run_keyword(self, args, kwargs=None):
        args, kwargs = self._handle_binary_args(args, kwargs or {})
        result = KeywordResult()
        self._intercept_std_streams()
        try:
            return_value = self._keyword(*args, **kwargs)
        except Exception:
            result.set_error(*sys.exc_info())
        else:
            try:
                result.set_return(return_value)
            except Exception:
                result.set_error(*sys.exc_info()[:2])
            else:
                result.set_status('PASS')
        finally:
            result.set_output(self._restore_std_streams())
        return result.data

    def _handle_binary_args(self, args, kwargs):
        args = [self._handle_binary_arg(a) for a in args]
        kwargs = dict((k, self._handle_binary_arg(v)) for k, v in kwargs.items())
        return args, kwargs

    def _handle_binary_arg(self, arg):
        return arg if not isinstance(arg, Binary) else arg.data

    def _intercept_std_streams(self):
        sys.stdout = StringIO()
        sys.stderr = StringIO()

    def _restore_std_streams(self):
        stdout = sys.stdout.getvalue()
        stderr = sys.stderr.getvalue()
        close = [sys.stdout, sys.stderr]
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        for stream in close:
            stream.close()
        if stdout and stderr:
            if not stderr.startswith(('*TRACE*', '*DEBUG*', '*INFO*', '*HTML*',
                                      '*WARN*', '*ERROR*')):
                stderr = '*INFO* %s' % stderr
            if not stdout.endswith('\n'):
                stdout += '\n'
        return stdout + stderr


class KeywordResult(object):
    _generic_exceptions = (AssertionError, RuntimeError, Exception)

    def __init__(self):
        self.data = {'status': 'FAIL'}

    def set_error(self, exc_type, exc_value, exc_tb=None):
        self._add('error', self._get_error_message(exc_type, exc_value))
        if exc_tb:
            self._add('traceback', self._get_error_traceback(exc_tb))
        self._add('continuable', self._get_error_attribute(exc_value, 'CONTINUE'),
                  default=False)
        self._add('fatal', self._get_error_attribute(exc_value, 'EXIT'),
                  default=False)

    def _add(self, key, value, default=''):
        if value != default:
            self.data[key] = value

    def _get_error_message(self, exc_type, exc_value):
        name = exc_type.__name__
        message = self._get_message_from_exception(exc_value)
        if not message:
            return name
        if exc_type in self._generic_exceptions \
                or getattr(exc_value, 'ROBOT_SUPPRESS_NAME', False):
            return message
        return '%s: %s' % (name, message)

    def _get_message_from_exception(self, value):
        # UnicodeError occurs below 2.6 and if message contains non-ASCII bytes
        # TODO: Can try/except be removed here?
        try:
            msg = unicode(value)
        except UnicodeError:
            msg = ' '.join(self._str(a, handle_binary=False) for a in value.args)
        return self._handle_binary_result(msg)

    def _get_error_traceback(self, exc_tb):
        # Latest entry originates from this class so it can be removed
        entries = traceback.extract_tb(exc_tb)[1:]
        trace = ''.join(traceback.format_list(entries))
        return 'Traceback (most recent call last):\n' + trace

    def _get_error_attribute(self, exc_value, name):
        return bool(getattr(exc_value, 'ROBOT_%s_ON_FAILURE' % name, False))

    def set_return(self, value):
        self._add('return', self._handle_return_value(value))

    def _handle_return_value(self, ret):
        if isinstance(ret, (str, unicode, bytes)):
            return self._handle_binary_result(ret)
        if isinstance(ret, (int, long, float)):
            return ret
        if isinstance(ret, Mapping):
            return dict((self._str(key), self._handle_return_value(value))
                        for key, value in ret.items())
        try:
            return [self._handle_return_value(item) for item in ret]
        except TypeError:
            return self._str(ret)

    def _handle_binary_result(self, result):
        if not self._contains_binary(result):
            return result
        if not isinstance(result, bytes):
            try:
                result = result.encode('ASCII')
            except UnicodeError:
                raise ValueError("Cannot represent %r as binary." % result)
        # With IronPython Binary cannot be sent if it contains "real" bytes.
        if sys.platform == 'cli':
            result = str(result)
        return Binary(result)

    def _contains_binary(self, result):
        if PY3:
            return isinstance(result, bytes) or BINARY.search(result)
        return (isinstance(result, bytes) and NON_ASCII.search(result) or
                BINARY.search(result))

    def _str(self, item, handle_binary=True):
        if item is None:
            return ''
        if not isinstance(item, (str, unicode, bytes)):
            item = unicode(item)
        if handle_binary:
            item = self._handle_binary_result(item)
        return item

    def set_status(self, status):
        self.data['status'] = status

    def set_output(self, output):
        if output:
            self.data['output'] = self._handle_binary_result(output)


if __name__ == '__main__':

    def stop(uri):
        server = test(uri, log_success=False)
        if server is not None:
            print('Stopping remote server at %s.' % uri)
            server.stop_remote_server()

    def test(uri, log_success=True):
        server = ServerProxy(uri)
        try:
            server.get_keyword_names()
        except:
            print('No remote server running at %s.' % uri)
            return None
        if log_success:
            print('Remote server running at %s.' % uri)
        return server

    def parse_args(args):
        actions = {'stop': stop, 'test': test}
        if not args or len(args) > 2 or args[0] not in actions:
            sys.exit('Usage:  python -m robotremoteserver test|stop [uri]')
        uri = args[1] if len(args) == 2 else 'http://127.0.0.1:8270'
        if '://' not in uri:
            uri = 'http://' + uri
        return actions[args[0]], uri

    action, uri = parse_args(sys.argv[1:])
    action(uri)
