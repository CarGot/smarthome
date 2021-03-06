#!/usr/bin/env python3
# vim: set encoding=utf-8 tabstop=4 softtabstop=4 shiftwidth=4 expandtab
#########################################################################
# Copyright 2011-2014 Marcus Popp                          marcus@popp.mx
# Copyright 2016-     Christian Strassburg            c.strassburg@gmx.de
# Copyright 2016-     Martin Sinn                           m.sinn@gmx.de
#########################################################################
#  This file is part of SmartHomeNG.
#  https://github.com/smarthomeNG/smarthome
#
#  SmartHomeNG is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  SmartHomeNG is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with SmartHomeNG. If not, see <http://www.gnu.org/licenses/>.
#########################################################################

#####################################################################
# Check Python Version
#####################################################################
import sys
if sys.hexversion < 0x03020000:
    print("Sorry your python interpreter ({0}.{1}) is too old. Please update to 3.2 or newer.".format(sys.version_info[0], sys.version_info[1]))
    exit()

#####################################################################
# prevent user root
#####################################################################
import os
if not os.name == 'nt':
    # only check if we are not at windows systems
    if os.getegid() == 0:
        print("SmartHomeNG should not run as root")
        # exit()

#####################################################################
# Import Python Core Modules
#####################################################################
import argparse
import datetime
import gc
import locale
import logging
import logging.handlers
import logging.config
import shutil
import re
import signal
import subprocess
import threading
import time
import traceback
import psutil
#####################################################################
# Base
#####################################################################
BASE = os.path.sep.join(os.path.realpath(__file__).split(os.path.sep)[:-2])
sys.path.insert(0, BASE)
PIDFILE= os.path.join(BASE,'var','run','smarthome.pid')
#####################################################################
# Import 3rd Party Modules
#####################################################################
from dateutil.tz import gettz

#####################################################################
# Import SmartHomeNG Modules
#####################################################################
import lib.config
import lib.connection
import lib.daemon
import lib.item
import lib.log
import lib.logic
import lib.plugin
import lib.scene
import lib.scheduler
import lib.tools
import lib.utils
import lib.orb
import lib.shyaml

from lib.constants import (YAML_FILE, CONF_FILE, DEFAULT_FILE)

#####################################################################
# Globals
#####################################################################
MODE = 'default'
VERSION = '1.3.'
TZ = gettz('UTC')
try:
    os.chdir(BASE)
    commit = subprocess.check_output(['git', 'describe', '--always'], stderr=subprocess.STDOUT).decode().strip('\n')
    VERSION += commit
    branch = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], stderr=subprocess.STDOUT).decode().strip('\n')
    if branch != 'master':
        VERSION += ".dev"
except Exception as e:
    print(e)
    VERSION += '0.man'


#####################################################################
# Classes
#####################################################################

class LogHandler(logging.StreamHandler):
    def __init__(self, log):
        logging.StreamHandler.__init__(self)
        self._log = log

    def emit(self, record):
        timestamp = datetime.datetime.fromtimestamp(record.created, TZ)
        self._log.add([timestamp, record.threadName, record.levelname, record.message])


class SmartHome():

    base_dir = BASE
    _etc_dir = os.path.join(base_dir, 'etc')
    _var_dir = os.path.join(base_dir, 'var')
    _lib_dir = os.path.join(base_dir,'lib')
    _env_dir = os.path.join(_lib_dir, 'env' + os.path.sep)

    _plugin_conf_basename = os.path.join(_etc_dir,'plugin')

    _plugin_conf = ''	# is filled by plugin.py while reading the configuration file, needed by Backend plugin

    _env_logic_conf_basename = os.path.join( _env_dir ,'logic')
    _items_dir = os.path.join(base_dir, 'items'+os.path.sep)
    _logic_conf_basename = os.path.join(_etc_dir, 'logic')
    _logic_dir = os.path.join(base_dir, 'logics'+os.path.sep)
    _cache_dir = os.path.join(_var_dir,'cache'+os.path.sep)
    _log_config = os.path.join(_etc_dir,'logging'+YAML_FILE)
    _smarthome_conf_basename = None

    _log_buffer = 50
    __logs = {}
    __event_listeners = {}
    __all_listeners = []
    _plugins = []
    __items = []
    __children = []
    __item_dict = {}
    _utctz = TZ
    logger = logging.getLogger(__name__)

    def __init__(self, smarthome_conf_basename=os.path.join(_etc_dir,'smarthome')):
        # set default timezone to UTC
        self._smarthome_conf_basename = smarthome_conf_basename
        global TZ
        self.tz = 'UTC'
        os.environ['TZ'] = self.tz
        self._tzinfo = TZ

        threading.currentThread().name = 'Main'
        self.alive = True
        self.version = VERSION
        self.connections = []

        # Fork process and write pidfile
        if MODE == 'default':
            lib.daemon.daemonize(PIDFILE)

        # Add Signal Handling
        signal.signal(signal.SIGHUP, self.reload_logics)
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        # check config files
        self.checkConfigFiles()
        # setup logging
        self.initLogging()

        #############################################################
        # Check Time
        while datetime.date.today().isoformat() < '2014-03-16':  # XXX update date
            time.sleep(5)
            print("Waiting for updated time.")
            self.logger.info("Waiting for updated time.")

        #############################################################
        # Catching Exceptions
        sys.excepthook = self._excepthook

        #############################################################
        # Reading smarthome.yaml

        config = lib.config.parse_basename(smarthome_conf_basename, configtype='SmartHomeNG')
        if config != {}:
            for attr in config:
                if not isinstance(config[attr], dict):  # ignore sub items
                    vars(self)['_' + attr] = config[attr]
            del(config)  # clean up

        if hasattr(self, '_module_paths'):
            sys.path.extend(self._module_paths if type(self._module_paths) is list else [self._module_paths])

        #############################################################
        # Setting debug level and adding memory handler
        self.initMemLog()

        #############################################################
        # Setting (local) tz if set in smarthome.conf
        if hasattr(self, '_tz'):
            tzinfo = gettz(self._tz)
            if tzinfo is not None:
                TZ = tzinfo
                self.tz = self._tz
                os.environ['TZ'] = self.tz
                self._tzinfo = TZ
            else:
                self.logger.warning("Problem parsing timezone: {}. Using UTC.".format(self._tz))
            del(self._tz, tzinfo)

        self.logger.warning("--------------------   Init SmartHomeNG {0}   --------------------".format(VERSION))
        self.logger.debug("Python {0}".format(sys.version.split()[0]))
        self._starttime = datetime.datetime.now()

        #############################################################
        # Link Tools
        self.tools = lib.tools.Tools()

        #############################################################
        # Link Sun and Moon
        self.sun = False
        self.moon = False
        if lib.orb.ephem is None:
            self.logger.warning("Could not find/use ephem!")
        elif not hasattr(self, '_lon') and hasattr(self, '_lat'):
            self.logger.warning('No latitude/longitude specified => you could not use the sun and moon object.')
        else:
            if not hasattr(self, '_elev'):
                self._elev = None
            self.sun = lib.orb.Orb('sun', self._lon, self._lat, self._elev)
            self.moon = lib.orb.Orb('moon', self._lon, self._lat, self._elev)

    def checkConfigFiles(self):
        # logging
        if not (os.path.isfile(self._log_config)):
            if os.path.isfile(self._log_config + DEFAULT_FILE):
                shutil.copy2(self._log_config + DEFAULT_FILE, self._log_config)
        # smarthome.yaml
        if not (os.path.isfile(self._smarthome_conf_basename + YAML_FILE)) and not (
                os.path.isfile(self._smarthome_conf_basename + CONF_FILE)):
            if os.path.isfile(self._smarthome_conf_basename + YAML_FILE + DEFAULT_FILE):
                shutil.copy2(self._smarthome_conf_basename + YAML_FILE + DEFAULT_FILE,
                             self._smarthome_conf_basename + YAML_FILE)
        # plugins
        if not (os.path.isfile(self._plugin_conf_basename + YAML_FILE)) and not (
                os.path.isfile(self._plugin_conf_basename + CONF_FILE)):
            if os.path.isfile(self._plugin_conf_basename + YAML_FILE + DEFAULT_FILE):
                shutil.copy2(self._plugin_conf_basename + YAML_FILE + DEFAULT_FILE,
                             self._plugin_conf_basename + YAML_FILE)

    def initLogging(self):
        fo = open(self._log_config, 'r')
        doc = lib.shyaml.yaml_load(self._log_config, False)
        logging.config.dictConfig(doc)
        fo.close()
        if MODE == 'interactive':  # remove default stream handler
            logging.getLogger().disabled = True
        elif MODE == 'debug':
            logging.getLogger().setLevel(logging.DEBUG)
        elif MODE == 'quiet':
            logging.getLogger().setLevel(logging.WARNING)
#       log_file.doRollover()

    def initMemLog(self):
        self.log = lib.log.Log(self, 'env.core.log', ['time', 'thread', 'level', 'message'], maxlen=self._log_buffer)
        _logdate = "%Y-%m-%d %H:%M:%S"
        _logformat = "%(asctime)s %(levelname)-8s %(threadName)-12s %(message)s"
        formatter = logging.Formatter(_logformat, _logdate)
        log_mem = LogHandler(self.log)
        log_mem.setLevel(logging.WARNING)
        log_mem.setFormatter(formatter)
        logging.getLogger('').addHandler(log_mem)
    #################################################################
    # Process Methods
    #################################################################

    def start(self):
        threading.currentThread().name = 'Main'

        #############################################################
        # Start Scheduler
        #############################################################
        self.scheduler = lib.scheduler.Scheduler(self)
        self.trigger = self.scheduler.trigger
        self.scheduler.start()

        #############################################################
        # Init Connections
        #############################################################
        self.connections = lib.connection.Connections()

        #############################################################
        # Init Plugins
        #############################################################
        self.logger.info("Init Plugins")
        self._plugins = lib.plugin.Plugins(self, configfile=self._plugin_conf_basename)

        #############################################################
        # Init Items
        #############################################################
        self.logger.info("Init Items")
        item_conf = None
        item_conf = lib.config.parse_itemsdir(self._env_dir, item_conf)
        item_conf = lib.config.parse_itemsdir(self._items_dir, item_conf)
        for attr, value in item_conf.items():
            if isinstance(value, dict):
                child_path = attr
                try:
                    child = lib.item.Item(self, self, child_path, value)
                except Exception as e:
                    self.logger.error("Item {}: problem creating: ()".format(child_path, e))
                else:
                    vars(self)[attr] = child
                    self.add_item(child_path, child)
                    self.__children.append(child)
        del(item_conf)  # clean up
        for item in self.return_items():
            item._init_prerun()
        for item in self.return_items():
            item._init_run()
        self.item_count = len(self.__items)
        self.logger.info("Items: {}".format(self.item_count))

        #############################################################
        # Init Logics
        #############################################################
        self._logics = lib.logic.Logics(self, self._logic_conf_basename, self._env_logic_conf_basename)

        #############################################################
        # Init Scenes
        #############################################################
        lib.scene.Scenes(self)

        #############################################################
        # Start Connections
        #############################################################
        self.scheduler.add('Connections', self.connections.check, cycle=10, offset=0)

        #############################################################
        # Start Plugins
        #############################################################
        self._plugins.start()

        #############################################################
        # Execute Maintenance Method
        #############################################################
        self.scheduler.add('sh.gc', self._maintenance, prio=8, cron=['init', '4 2 * *'], offset=0)

        #############################################################
        # Main Loop
        #############################################################
        while self.alive:
            try:
                self.connections.poll()
            except Exception as e:
                self.logger.exception("Connection polling failed: {}".format(e))

    def stop(self, signum=None, frame=None):
        self.alive = False
        self.logger.info("Number of Threads: {0}".format(threading.activeCount()))
        for item in self.__items:
            self.__item_dict[item]._fading = False
        try:
            self.scheduler.stop()
        except:
            pass
        try:
            self._plugins.stop()
        except:
            pass
        try:
            self.connections.close()
        except:
            pass
        for thread in threading.enumerate():
            try:
                thread.join(1)
            except:
                pass
        if threading.active_count() > 1:
            for thread in threading.enumerate():
                self.logger.info("Thread: {}, still alive".format(thread.name))
        else:
            self.logger.info("SmartHomeNG stopped")
        lib.daemon.remove_pidfile(PIDFILE)

        logging.shutdown()
        exit()

    #################################################################
    # Item Methods
    #################################################################
    def __iter__(self):
        for child in self.__children:
            yield child

    def add_item(self, path, item):
        if path not in self.__items:
            self.__items.append(path)
        self.__item_dict[path] = item

    def return_item(self, string):
        if string in self.__items:
            return self.__item_dict[string]

    def return_items(self):
        for item in self.__items:
            yield self.__item_dict[item]

    def match_items(self, regex):
        regex, __, attr = regex.partition(':')
        regex = regex.replace('.', '\.').replace('*', '.*') + '$'
        regex = re.compile(regex)
        attr, __, val = attr.partition('[')
        val = val.rstrip(']')
        if attr != '' and val != '':
            return [self.__item_dict[item] for item in self.__items if regex.match(item) and attr in self.__item_dict[item].conf and ((type(self.__item_dict[item].conf[attr]) in [list,dict] and val in self.__item_dict[item].conf[attr]) or (val == self.__item_dict[item].conf[attr]))]
        elif attr != '':
            return [self.__item_dict[item] for item in self.__items if regex.match(item) and attr in self.__item_dict[item].conf]
        else:
            return [self.__item_dict[item] for item in self.__items if regex.match(item)]

    def find_items(self, conf):
        for item in self.__items:
            if conf in self.__item_dict[item].conf:
                yield self.__item_dict[item]

    def find_children(self, parent, conf):
        children = []
        for item in parent:
            if conf in item.conf:
                children.append(item)
            children += self.find_children(item, conf)
        return children

    #################################################################
    # Plugin Methods
    #################################################################
    def return_plugins(self):
        for plugin in self._plugins:
            yield plugin

    #################################################################
    # Logic Methods
    #################################################################
    def reload_logics(self, signum=None, frame=None):
        for logic in self._logics:
            self._logics[logic].generate_bytecode()

    def return_logic(self, name):
        return self._logics[name]

    def return_logics(self):
        for logic in self._logics:
            yield logic

    #################################################################
    # Log Methods
    #################################################################
    def add_log(self, name, log):
        self.__logs[name] = log

    def return_logs(self):
        return self.__logs

    #################################################################
    # Event Methods
    #################################################################
    def add_event_listener(self, events, method):
        for event in events:
            if event in self.__event_listeners:
                self.__event_listeners[event].append(method)
            else:
                self.__event_listeners[event] = [method]
        self.__all_listeners.append(method)

    def return_event_listeners(self, event='all'):
        if event == 'all':
            return self.__all_listeners
        elif event in self.__event_listeners:
            return self.__event_listeners[event]
        else:
            return []

    #################################################################
    # Time Methods
    #################################################################
    def now(self):
        # tz aware 'localtime'
        return datetime.datetime.now(self._tzinfo)

    def tzinfo(self):
        return self._tzinfo

    def utcnow(self):
        # tz aware utc time
        return datetime.datetime.now(self._utctz)

    def utcinfo(self):
        return self._utctz

    def runtime(self):
        return datetime.datetime.now() - self._starttime

    #################################################################
    # Helper Methods
    #################################################################
    def _maintenance(self):
        self._garbage_collection()
        references = sum(self._object_refcount().values())
        self.logger.debug("Object references: {}".format(references))

    def _excepthook(self, typ, value, tb):
        mytb = "".join(traceback.format_tb(tb))
        self.logger.error("Unhandled exception: {1}\n{0}\n{2}".format(typ, value, mytb))

    def _garbage_collection(self):
        c = gc.collect()
        self.logger.debug("Garbage collector: collected {0} objects.".format(c))

    # obsolete by utils.
    def string2bool(self, string):
        try:
            return lib.utils.Utils.to_bool(string)
        except Exception as e:
            return None


    def object_refcount(self):
        objects = self._object_refcount()
        objects = [(x[1], x[0]) for x in list(objects.items())]
        objects.sort(reverse=True)
        return objects

    def _object_refcount(self):
        objects = {}
        for module in list(sys.modules.values()):
            for sym in dir(module):
                try:
                    obj = getattr(module, sym)
                    if isinstance(obj, type):
                        objects[obj] = sys.getrefcount(obj)
                except:
                    pass
        return objects


#####################################################################
# Private Methods
#####################################################################

def reload_logics():
    pid = lib.daemon.read_pidfile(PIDFILE)
    if pid:
        os.kill(pid, signal.SIGHUP)


#####################################################################
# Main
#####################################################################

if __name__ == '__main__':
    if locale.getdefaultlocale() == (None, None):
        locale.setlocale(locale.LC_ALL, 'C')
    else:
        locale.setlocale(locale.LC_ALL, '')

    # argument handling
    argparser = argparse.ArgumentParser()
    arggroup = argparser.add_mutually_exclusive_group()
    arggroup.add_argument('-v', '--verbose', help='DEPRECATED use logging.config (verbose (debug output) logging to the logfile)', action='store_true')
    arggroup.add_argument('-d', '--debug', help='stay in the foreground with verbose output', action='store_true')
    arggroup.add_argument('-i', '--interactive', help='open an interactive shell with tab completion and with verbose logging to the logfile', action='store_true')
    arggroup.add_argument('-l', '--logics', help='reload all logics', action='store_true')
    arggroup.add_argument('-s', '--stop', help='stop SmartHomeNG', action='store_true')
    arggroup.add_argument('-q', '--quiet', help='DEPRECATED use logging config (reduce logging to the logfile)', action='store_true')
    arggroup.add_argument('-V', '--version', help='show SmartHomeNG version', action='store_true')
    arggroup.add_argument('--start', help='start SmartHomeNG and detach from console (default)', default=True, action='store_true')
    args = argparser.parse_args()

    if args.interactive:
        MODE = 'interactive'
        import code
        import rlcompleter  # noqa
        import readline
        import atexit
        # history file
        histfile = os.path.join(os.environ['HOME'], '.history.python')
        try:
            readline.read_history_file(histfile)
        except IOError:
            pass
        atexit.register(readline.write_history_file, histfile)
        readline.parse_and_bind("tab: complete")
        sh = SmartHome()
        _sh_thread = threading.Thread(target=sh.start)
        _sh_thread.start()
        shell = code.InteractiveConsole(locals())
        shell.interact()
        exit(0)
    elif args.logics:
        reload_logics()
        exit(0)
    elif args.version:
        print("{0}".format(VERSION))
        exit(0)
    elif args.stop:
        lib.daemon.kill(PIDFILE)
        exit(0)
    elif args.debug:
        MODE = 'debug'
    elif args.quiet:
        pass
    elif args.verbose:
        pass

    # check for pid file
    if lib.daemon.check_sh_is_running(PIDFILE):
        print("SmartHomeNG already running with pid {}".format(lib.daemon.read_pidfile(PIDFILE)))
        print("Run 'smarthome.py -s' to stop it.")
        exit()
    if MODE == 'debug':
        lib.daemon.write_pidfile(psutil.Process().pid, PIDFILE)
    # Starting SmartHomeNG
    sh = SmartHome()
    sh.start()
