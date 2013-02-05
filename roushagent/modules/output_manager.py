#!/usr/bin/env python

import os
import logging
import socket
from functools import partial

LOG = logging.getLogger('roush.output')

# output modules recieve an input action, and return an output
# result.  Generally they take the form of actions to perform.
#
# output plugins *must* export a "name" value.  In addition,
# they *must* export a "setup" function, which takes a config hash.
# The config hash will be merely the config items in the section
# of the main configfile named the same as the "name" value exported
# by the plugin.
#
# when the setup function is called, it should register actions
# that it is willing to handle.  It can use the "register_action()"
# function exported into the module namespace to do so.
#
# other items injected into module namespace:
#
# LOG - a python logging handler
# global_config - the global config hash
# module_config - the configuration for the module
# register_action()
#
# after registering an action, any incoming data sent to
# a specific action will be sent to the registered dispatch
# handler, as registered by the module.
#
# The dispatch functions will receive a python dict with two items:
#
# "id": a unique transaction id (generated by the input module)
# "action": the action that that caused the dispatch to be called
# "payload": the input dict recieved from the input module
#
# The payload is arbitrary, and is specific to the action.
#
# The dispatch handler should processes the message, and return
# a python dict in the following format:
#
# {
#   'result_code': <result-code-ish>
#   'result_str': <error-or-success-message>
#   'result_data': <extended error info or arbitrary data> }
#


class OutputManager:
    def __init__(self, path, config={}):
        # Load all available plugins, or those
        # specified by the config.
        self.output_plugins = {}
        # should all actions be named module.action?
        self.loaded_modules = ['modules']
        self.dispatch_table = {
            'logfile.tail': [
                self.handle_logfile,
                'modules', 'modules', 'modules', [], [], {}],
            'modules.list': [
                self.handle_modules,
                'modules', 'modules', 'modules', [], [], {}],
            'modules.load': [
                self.handle_modules,
                'modules', 'modules', 'modules', [], [], {}],
            'modules.actions': [
                self.handle_modules,
                'modules', 'modules', 'modules', [], [], {}],
            'modules.reload': [
                self.handle_modules,
                'modules', 'modules', 'modules', [], [], {}]}
        self.config = config
        self.load(path)

        LOG.debug('Dispatch methods: %s' % self.dispatch_table.keys())

    def load(self, path):
        # Load a plugin by file name.  modules with
        # action_foo methods will be auto-registered
        # for the 'foo' action
        if type(path) == list:
            for d in path:
                self.load(d)
        else:
            if os.path.isdir(path):
                self._load_directory(path)
            else:
                self._load_file(path)

    def _load_directory(self, path):
        LOG.debug('Preparing to load output modules in directory %s' % path)
        dirlist = os.listdir(path)
        for relpath in dirlist:
            p = os.path.join(path, relpath)

            if not os.path.isdir(p) and p.endswith('.py'):
                self._load_file(p)

    def _load_file(self, path):
        self.shortpath = shortpath = os.path.basename(path)

        # we can't really load this into the existing namespace --
        # we'll have registration collisions.
        ns = {'global_config': self.config,
              'LOG': LOG}
        LOG.debug('Loading output plugin file %s' % shortpath)
        execfile(path, ns)

        if not 'name' in ns:
            LOG.warning('Plugin missing "name" value. Ignoring.')
            return
        name = ns['name']
        # getChild is only available on python2.7
        # ns['LOG'] = ns['LOG'].getChild('output_%s' % name)
        ns['LOG'] = logging.getLogger('%s.%s' % (ns['LOG'],
                                                 'output_%s' % name))
        ns['register_action'] = partial(self.register_action, name)

        self.loaded_modules.append(name)
        self.output_plugins[name] = ns
        config = self.config.get(name, {})
        ns['module_config'] = config
        if 'setup' in ns:
            ns['setup'](config)
        else:
            LOG.warning('No setup function in %s. Ignoring.' % shortpath)

    def register_action(self, plugin, action, method,
                        constraints=[],
                        consequences=[],
                        args={}):
        LOG.debug('Registering handler for action %s' % action)
        # First handler wins
        if action in self.dispatch_table:
            _, path, name = self.dispatch_table[action]
            raise NameError('Action %s already registered to %s:%s' % (action,
                                                                       path,
                                                                       name))
        else:
            self.dispatch_table[action] = (method, self.shortpath,
                                           method.func_name,
                                           plugin,
                                           constraints,
                                           consequences,
                                           args)

    def actions(self):
        d = {}
        for k, v in self.dispatch_table.items():
            d[k] = {'plugin': v[3],
                    'constraints': v[4],
                    'consequences': v[5],
                    'args': v[6]}
        return d

    def dispatch(self, input_data):
        # look at the dispatch table for matching actions
        # and dispatch them in order to the registered
        # handlers.
        #
        # Not sure what exactly to do with multiple
        # registrations for the same event, so we'll
        # punt and just pass to the first successful.
        #
        action = input_data['action']
        result = {'result_code': 253,
                  'result_str': 'no dispatcher found for action "%s"' % action,
                  'result_data': ''}
        if action in self.dispatch_table:
            fn, path, _, plugin, _, _, _ = self.dispatch_table[action]
            LOG.debug('Plugin_manager: dispatching action %s from plugin %s' %
                      (action, plugin))
            LOG.debug('Received input_data %s' % (input_data))
            base = self.config['main'].get('trans_log_dir', '/var/log/roush')

            if not os.path.isdir(base):
                raise OSError(2, 'Specified path "%s" ' % (base) +
                              'does not exist or is not a directory.')

            if not os.access(base, os.W_OK):
                raise OSError(13,
                              'Specified path "%s" is not writable.' %
                              base)

            # we won't log from built-in functions
            ns = None
            if plugin in self.output_plugins:
                ns = self.output_plugins[plugin]
                t_LOG = ns['LOG']
                if 'id' in input_data:
                    ns['LOG'] = logging.getLogger(
                        'roush.output.trans_%s' % input_data['id'])
                    h = logging.FileHandler(os.path.join(base, 'trans_%s.log' %
                                                         input_data['id']))
                    ns['LOG'].addHandler(h)

            # FIXME(rp): handle exceptions
            result = fn(input_data)

            if ns is not None:
                ns['LOG'] = t_LOG

            LOG.debug('Got result %s' % result)
        else:
            if action.startswith('rollback_'):
                result = {'result_code': 0,
                          'result_str': 'no rollback action for %s' % action,
                          'result_data': {}}
            else:
                LOG.warning('No dispatch for action "%s"' % action)
        return result

    # some internal methods to provide some agent introspection
    def handle_logfile(self, input_data):
        def _ok(code=0, message='success', data={}):
            return {'result_code': code,
                    'result_str': message,
                    'result_data': data}

        def _fail(code=2, message='unknown failure', data={}):
            return _ok(code, message, data)

        action = input_data['action']
        payload = input_data['payload']

        if not 'task_id' in payload or \
                not 'dest_ip' in payload or \
                not 'dest_port' in payload:
            return _fail(message='must specify task_id, '
                         'dest_ip and dest_port')

        if action == 'logfile.tail':
            base = self.config['main'].get('trans_log_dir', '/var/log/roush')
            log_path = os.path.join(base, 'trans_%s.log' % payload['task_id'])

            data = ''
            with open(log_path, 'r') as fd:
                fd.seek(0, os.SEEK_END)
                length = fd.tell()
                fd.seek(max((length-1024, 0)))
                data = fd.read()

            # open the socket and jet it out
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

            try:
                sock.connect((payload['dest_ip'],
                              int(payload['dest_port'])))
            except socket.error as e:
                sock.close()
                return _fail(message='%s' % str(e))

            sock.send(data)
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
            return _ok()

    def handle_modules(self, input_data):
        def _ok(code=0, message='success', data={}):
            return {'result_code': code,
                    'result_str': message,
                    'result_data': data}

        def _fail(code=2, message='unknown failure', data={}):
            return _ok(code, message, data)

        action = input_data['action']
        payload = input_data['payload']
        result_code = 1
        result_str = 'failed to perform action'
        result_data = ''
        if action == 'modules.list':
            return _ok(data={'name': 'roush_agent_output_modules',
                                  'value': self.loaded_modules})
        elif action == 'modules.actions':
            return _ok(data={'name': 'roush_agent_actions',
                                  'value': self.actions()})
        elif action == 'modules.load':
            if not 'path' in payload:
                return _fail(message='no "path" specified in payload')
            elif not os.path.isfile(payload['path']):
                return _fail(message='specified module does not exist')
            else:
                # any exceptions we'll bubble up from the manager
                self.loadfile(payload['path'])
        elif action == 'modules.reload':
            pass
        return _ok()

    def stop(self):
        for plugin in self.output_plugins:
            if 'teardown' in self.output_plugins[plugin]:
                self.output_plugins[plugin]['teardown']()
