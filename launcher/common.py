#!/usr/bin/env python3
# coding: utf-8

import os
import sys
import ssl
import time
import socket
from urllib.request import urlopen, Request

def get_dirname(path):
    return os.path.dirname(os.path.realpath(path))

file_dir = get_dirname(__file__)
root_dir = os.path.dirname(file_dir)
py_dir = os.path.join(root_dir, 'python')
app_start = os.path.join(root_dir, 'start.py')
icon_gotox = os.path.join(root_dir, 'gotox.ico')
config_dir = os.path.join(root_dir, 'config')
direct_ipdb = os.path.join(root_dir, 'data', 'directip.db')
direct_domains = os.path.join(root_dir, 'data', 'directdomains.txt')
config_filename = os.path.join(config_dir, 'Config.ini')
config_user_filename = os.path.join(config_dir, 'Config.user.ini')
config_auto_filename = os.path.join(config_dir, 'ActionFilter.ini')
# GotoX CA
ca1 = os.path.join(root_dir, 'cert', 'CA.crt')
# APNIC 和 GitHub 使用的 CA
ca2 = os.path.join(root_dir, 'cert', 'cacert-ds.pem')
context = None
logging = None


if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from local.compat import replace_logging, patch_configparser

def load_config():
    patch_configparser()
    import re
    from configparser import ConfigParser

    _LOGLv = {
        0 : logging.WARNING,
        1 : logging.INFO,
        2 : logging.TEST,
        3 : logging.DEBUG
        }

    CONFIG = ConfigParser(dict_type=dict, inline_comment_prefixes=('#', ';'))
    CONFIG._optcre = re.compile(r'(?P<option>[^=\s]+)\s*(?P<vi>=?)\s*(?P<value>.*)')
    CONFIG.read([config_filename, config_user_filename])
    LISTEN_IP = CONFIG.get('listen', 'ip')
    if LISTEN_IP == '0.0.0.0':
        LISTEN_IP = '127.0.0.1'
    elif LISTEN_IP == '::':
        LISTEN_IP = '::1'
    elif LISTEN_IP == '':
        LINK_PROFILE = CONFIG.get('link', 'profile')
        if LINK_PROFILE not in ('ipv4', 'ipv6', 'ipv46'):
            LINK_PROFILE = 'ipv4'
        LISTEN_IP = '127.0.0.1' if '4' in LINK_PROFILE else '::1'
    LISTEN_GAE_PORT = CONFIG.getint('listen', 'gae_port', fallback=8086)
    LISTEN_AUTO_PORT = CONFIG.getint('listen', 'auto_port', fallback=8087)
    LISTEN_GAE = '%s:%d' % (LISTEN_IP, LISTEN_GAE_PORT)
    LISTEN_AUTO = '%s:%d' % (LISTEN_IP, LISTEN_AUTO_PORT)
    LOG_PRINT = CONFIG.getboolean('log', 'print', fallback=True)
    LOG_LEVEL = _LOGLv[min(CONFIG.getint('log', 'level', fallback=1), 3)]
    log_config = {'level': LOG_LEVEL}
    if not LOG_PRINT:
        log_config['stream'] = logging.NULL_STREAM
    logging.basicConfig(**log_config)
    return LISTEN_GAE, LISTEN_AUTO

def getlogger(use_print=False):
    global logging
    if logging is None:
        if use_print:
            class logging:
                warning = info = debug = print
        else:
            replace_logging()
            import logging
    return logging

try:
    startfile = os.startfile
except AttributeError:
    def startfile(filename):
        from subprocess import call
        if sys.platform.startswith('darwin'):
            operation = 'open'
        elif os.name == 'posix':
            operation = 'xdg-open'
        call((operation, filename))

class DataSource:
    datefmt = None

    def __init__(self, manager, name, url, parser, fullname=None):
        if isinstance(manager, DataSourceManager):
            self.parent = None
            self.__generations = 1
            self.__sign = 1 << manager.sign_bit
        elif isinstance(manager, self.__class__):
            parent = manager
            manager = parent.manager
            generations = parent.__generations + 1
            if generations > manager.max_generations:
                raise ValueError(
                        'DataSource.__init__ "generations=%d" 超过最大值：%d'
                        % (generations, manager.max_generations))
            self.__generations = generations
            self.parent = parent
            parent.add_ext(name)
            parent.__children[name.lower()] = self
            self.__sign = 0
            parser = parser or parent.parser
        else:
            raise TypeError('DataSource.__init__ "manager" 类型错误：%s'
                            % manager.__class__)
        self.manager = manager
        self.__name = name
        self.url = url
        self.parser = parser
        self.fullname = fullname or name
        self.req = None
        self.update = None
        self.itemlist = []
        self.ext = 0
        self.__extlist = {}
        self.__ext_bit = 0
        self.__children = {}

    def add_child(self, name, url, parser=None, fullname=None):
        return self.__class__(self, name, url, parser, fullname)

    def get_child(self, name):
        return self.__children.get(name.lower())

    def get_all_children(self):
        return self.__children.values()

    def add_ext(self, names):
        if isinstance(names, str):
            names = [names]
        for name in names:
            name = name.lower()
            if name in self.__extlist:
                continue
            self.__extlist[name] = 1 << self.__ext_bit
            self.__ext_bit += 1

    def check_ext(self, name):
        return self.ext & self.__extlist.get(name.lower(), 0)

    def set_ext(self, name, sign=1, save=False):
        if name not in self:
            self.add_ext(name)
        _ext = self.__extlist[name]
        if isinstance(sign, str):
            sign = sign.lower()
        if sign in (1, True, '1', 'on', 'yes', 'true'):
            self.ext |= _ext
        elif sign in (0, False, '0', 'off', 'no', 'false') and self.ext & _ext:
            self.ext ^= _ext
        if save:
            self.save_ext()

    def switch_ext(self, name, save=False):
        self.ext ^= self.__extlist[name.lower()]
        if save:
            self.save_ext()

    def get_index_name(self):
        if self.parent is None:
            return self.name
        else:
            return '%s.%s' % (self.parent.get_index_name(), self.name)

    def check_name(self, name):
        return name.lower() == self.get_index_name().lower()

    def load_ext(self, names=None, filename=None):
        if names:
            self.add_ext(names)
        if not filename:
            filename = self.ext_conf
        if os.path.exists(filename):
            with open(filename, 'r') as fd:
                for line in fd:
                    name, _, value = line.partition(':')
                    dsname, _, name = name.strip().rpartition('.')
                    if self.check_name(dsname):
                        self.set_ext(name, value.strip())

    def save_ext(self, filename=None):
        exts = []
        if not filename:
            filename = self.ext_conf
        if os.path.exists(filename):
            with open(filename, 'r') as fd:
                for line in fd:
                    name, _, _ = line.partition(':')
                    dsname, _, name = name.strip().rpartition('.')
                    if not self.check_name(dsname) or name not in self:
                        exts.append(line)
        for name in self.__extlist:
            ext = self.check_ext(name) and 1
            exts.append('%s.%s: %d\n' % (self.name.lower(), name, ext))
        with open(filename, 'w') as fd:
            for ext in exts:
                fd.write(ext)

    def __contains__(self, name):
        return name.lower() in self.__extlist

    @property
    def sign(self):
        return self.__sign

    @property
    def ext_conf(self):
        return self.manager.ext_conf

    @property
    def name(self):
        return self.__name

    @property
    def update(self):
        return '%s-%s' % (self.name, self.__update)

    @update.setter
    def update(self, value):
        self.__update = value

    def __get_other_sign(self, other):
        if isinstance(other, self.__class__):
            other = other.sign
        return other

    def __and__(self, other):
        return self.__get_other_sign(other) & self.sign

    def __xor__(self, other):
        return self.__get_other_sign(other) ^ self.sign

    def __or__(self, other):
        return self.__get_other_sign(other) | self.sign

    __rand__ = __and__
    __rxor__ = __xor__
    __ror__ = __or__

    def __raise_noit_err(self, other):
        raise NotImplementedError

    __iand__ = __ixor__ = __ior__ = __raise_noit_err

class DataSourceManager:
    ext_conf = os.path.join(root_dir, 'config', 'dsext.conf')
    max_generations = 2

    def __init__(self):
        self.__sign_all = 0
        self.__sign_bit = 0
        self.__valid = {}

    def add(self, name, url, parser, fullname=None):
        ds = DataSource(self, name, url, parser, fullname)
        self.__valid['--' + name.lower()] = ds
        self.__sign_all |= ds.sign
        self.__sign_bit += 1
        return ds

    def get(self, name):
        return self.__valid.get('--' + name.lower())

    @property
    def sign_bit(self):
        return self.__sign_bit

    @property
    def sign_all(self):
        return self.__sign_all

    def load_ext(self, filename=None):
        if filename:
            self.ext_conf = filename
        for _, ds in self.__valid.items():
            ds.load_ext()

    def save_ext(self, filename=None):
        if filename:
            self.ext_conf = filename
        for _, ds in self.__valid.items():
            ds.save_ext()

    def get_source(self, *args):
        kwargs = parse_cmds(*args)
        data_source = 0
        if '--all' in kwargs:
            data_source = self.__sign_all
        for par in self.__valid:
            if par in kwargs:
                data_source |= self.__valid[par].sign
                for name in kwargs[par]:
                    self.__valid[par].set_ext(name)
        return data_source

def parse_cmds(*args):
    args = list(args)
    kwargs = {}
    while args and not args[0].startswith('-'):
        del args[0]
    cmd = ''
    for arg in args:
        if arg.startswith('-'):
            cmd = arg
            kwargs[cmd] = []
        else:
            kwargs[cmd].append(arg)
    return kwargs

def download(req):
    #显式加载 CA，确保正常使用
    global context
    if context is None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLSv1)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.set_ciphers(ssl._RESTRICTED_SERVER_CIPHERS)
        if os.path.exists(ca1):
            context.load_verify_locations(ca1)
        context.load_verify_locations(ca2)
    retry_delay = 2
    max_retries = 10
    retry_times = 0
    timeout = 8
    l = 0
    while l is 0:
        fd = None
        err = None
        try:
            fd = urlopen(req, timeout=timeout, context=context)
            l = int(fd.headers.get('Content-Length', 0))
        except Exception as e:
            err = e
        if l is 0:
            if fd:
                fd.close()
            retry_times += 1
            if retry_times > max_retries:
                logging.warning('请求网址 %r 时，重试 %d 次后仍然失败。'
                                % (req.full_url, max_retries))
                logging.warning('请忽略下面这个错误跟踪，并检查是否需要'
                                '更改自动代理规则（ActionFilter.ini）。')
                raise err or OSError('连接失败', 0)
            logging.debug('获取直连数据网址失败，%d 秒后重试' % retry_delay)
            time.sleep(retry_delay)
    return fd, l

def download_as_list(ds):
    #支持续传
    logging.info('开始下载 %s 列表' % ds.fullname)
    if ds.req is None:
        ds.req = Request(ds.url)
    ds.req.headers['Range'] = 'bytes=0-'
    if ds.datefmt is None:
        ds.update = None
    else:
        ds.update = time.strftime(ds.datefmt, time.localtime(time.time()))
    ds.itemlist.clear()
    read = 0
    l = None
    while read != l:
        fd, _l = download(ds.req)
        if l is None:
            l = _l
        _read = ds.parser(fd, ds)
        if _read is None:
            read = l
        else:
            read += _read
        fd.close()
        #下载失败续传
        if read != l:
            #往回跳过可能的缺损条目
            read = max(read - 100, 0)
            ds.req.headers['Range'] = 'bytes=%d-' % read
            logging.debug('%s 列表下载中断，续传：%d/%d' % (ds.fullname, read, l))
    logging.info(ds.fullname + ' 列表下载完毕')
    return ds.itemlist

def set_proxy(proxy_addr):
    try:
        ip, port = proxy_addr.split(':')
        socket.create_connection((ip, int(port)), timeout=1).close()
        os.environ['HTTPS_PROXY'] = os.environ['HTTP_PROXY'] = proxy_addr
        logging.info('\n代理地址 %r 已设置成功。' % proxy_addr)
        return True
    except:
        os.environ.pop('HTTP_PROXY', None)
        os.environ.pop('HTTPS_PROXY', None)

def parse_set_proxy(data_source):
    if '-p' in sys.argv:
        try:
            proxy_addr = sys.argv[sys.argv.index('-p') + 1]
        except IndexError:
            print('\n代理地址读取失败，退出脚本...')
            sys.exit(-1)
        if set_proxy(proxy_addr):
            use_proxy = None
        else:
            print('\n代理地址 %r 设置失败，退出脚本...' % proxy_addr)
            sys.exit(-1)
        if data_source == 0:
            print('进入交互模式\n')
    elif '-d' in sys.argv:
        use_proxy = False
        if data_source == 0:
            print('进入交互模式\n')
    else:
        use_proxy = input('进入交互模式\n\n是否设置代理（Y/N）：')
        use_proxy = use_proxy.upper() == 'Y'

    if use_proxy:
        print('\n开始设置代理，仅支持 HTTP 代理，格式："主机名(IP 或域名):端口"')
    while use_proxy:
        proxy_addr = input('\n请输入代理地址，'
                     '留空使用 "127.0.0.1:8087"：\n') or '127.0.0.1:8087'
        if set_proxy(proxy_addr):
            break
        else:
            use_proxy = input('\n当前代理 %r 无法链接，是否继续设置代理（Y/N）：' % proxy_addr)
            use_proxy = use_proxy.upper() == 'Y'
    if use_proxy is False:
        print('\n跳过代理设置')
    return use_proxy

Tips1 = '''
 ***********************************************
 *   请选择存放目录：                          *
 *                      数据目录 ------ 按 1   *
 *                      当前目录 ------ 按 2   *
 *                      退出 ---------- 按 0   *
 ***********************************************
'''

def select_path(*path):
    n = input(Tips1)
    try:
        n = int(n)
    except:
        print('输入错误！')
        return
    if n is 0:
        sys.exit(0)
    elif n is 1:
        return path[0]
    elif n is 2:
        return path[1]
    else:
        print('输入错误！')
