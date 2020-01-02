"""
Jupyter <-> Vim
String Utility functions:
    1/ Helper (unquote_string)
    2/ Formater / Parser (parse_messages)
"""

import re
from sys import version_info
from os import listdir
from os.path import isfile, join, splitext
import vim

try:
    from queue import Queue
except ImportError:
    from Queue import Queue

# -----------------------------------------------------------------------------
#        Helpers
# -----------------------------------------------------------------------------


class VimMessenger():
    """Handle message to/from Vim"""
    def __init__(self):
        # Message queue: for async echom
        self.message_queue = Queue()
        # Pid of current vim section executing me
        self.pid = vim.eval('getpid()')
        # Number of column of vim section
        self.column = 80

    def set_column(self):
        """Set vim column number <- vim"""
        self.column = vim.eval('&columns')

    @staticmethod
    def get_timer_intervals():
        """Return list<int> timers in ms user defined"""
        vim_list = vim.bindeval('g:jupyter_timer_intervals')
        return [i for i in vim_list if isinstance(i, int)]

    def thread_echom(self, arg, **args):
        """Wrap echo async: put message to be echo in a queue """
        self.message_queue.put((arg, args))

    def timer_echom(self):
        """Call echom sync: all messages in queue"""
        # Check in
        if self.message_queue.empty(): return

        # Show user the force
        while not self.message_queue.empty():
            (arg, args) = self.message_queue.get_nowait()
            echom(arg, **args)

        # Restore peace in the galaxy
        vim.command('redraw')

    def string_hi(self):
        """Return Hi froom vim string"""
        return ('\\n\\nReceived connection from vim client with pid %d'
                '\\n' + '-' * 60 + '\\n').format(self.pid)

    def thread_echom_kernel_info(self, kernel_info, cfile_id):
        """Echo kernel info (async)
        Prettify output: appearance rules
        """
        from pprint import PrettyPrinter
        pp = PrettyPrinter(indent=4, width=self.column)
        kernel_string = pp.pformat(kernel_info)[4:-1]

        # # Echo message
        self.thread_echom('To: ', style='Question')
        self.thread_echom(kernel_string.replace('\"', '\\\"'), cmd='echom')

        # Send command so that user knows vim is connected at bottom, more readable
        self.thread_echom('Connected: {}'.format(cfile_id), style='Question')


def echom(arg, style="None", cmd='echom'):
    """Report string `arg` using vim's echomessage command.

    Keyword args:
    style -- the vim highlighting style to use
    """
    try:
        vim.command("echohl {}".format(style))
        messages = arg.split('\n')
        for msg in messages:
            vim.command(cmd + " \"{}\"".format(msg.replace('\"', '\\\"')))
        vim.command("echohl None")
    except vim.error:
        print("-- {}".format(arg))


def warn_no_connection():
    """Echo warning: not connected"""
    echom('WARNING: Not connected to Jupyter!'
          '\nRun :JupyterConnect to find the kernel', style='WarningMsg')


def str_to_py(var):
    """Convert: Vim -> Py"""
    is_py3 = version_info[0] >= 3
    encoding = vim.eval('&encoding') or 'utf-8'
    if is_py3 and isinstance(var, bytes):
        var = str(var, encoding)
    elif not is_py3 and isinstance(var, str):
        # pylint: disable=undefined-variable
        var = unicode(var, encoding)  # noqa: E0602
    return var


def str_to_vim(obj):
    """Convert: Py -> Vim
    Independant of vim's version
    """
    # Encode
    if version_info[0] < 3:
        # pylint: disable=undefined-variable
        obj = unicode(obj, 'utf-8')  # noqa: E0602
    else:
        if not isinstance(obj, bytes):
            obj = obj.encode()
        obj = str(obj, 'utf-8')

    # Vim cannot deal with zero bytes:
    obj = obj.replace('\0', '\\0')

    # Escape
    obj.replace('\\', '\\\\').replace('"', r'\"')

    return '"{:s}"'.format(obj)


def unquote_string(string):
    """Unquote some text/plain response from kernel"""
    res = str(string)
    for quote in ("'", '"'):
        res = res.rstrip(quote).lstrip(quote)
    return res


def strip_color_escapes(s):
    """Remove ANSI color escape sequences from a string."""
    re_strip_ansi = re.compile(r'\x1B\[([0-9]{1,2}(;[0-9]{1,2})*)?[mK]')
    return re_strip_ansi.sub('', s)


def prettify_execute_intput(line_number, cmd, prompt_in):
    """Also used with my own input (as iperl does not send it back)"""
    prompt = prompt_in.format(line_number)
    s = prompt
    # add continuation line, if necessary
    dots = (' ' * (len(prompt.rstrip()) - 4)) + '...: '
    s += cmd.rstrip().replace('\n', '\n' + dots)
    return s


def shorten_filename(runtime_file):
    """Shorten connection filename kernel-24536.json -> 24536"""
    if runtime_file is None: return ''
    r_cfile = r'.*kernel-([0-9a-fA-F]*)[0-9a-fA-F\-]*.json'
    return re.sub(r_cfile, r'\1', runtime_file)


def find_jupyter_kernels():
    """Find opened kernels
    Called: <- vim completion method
    Returns: List of string
    """
    from jupyter_core.paths import jupyter_runtime_dir

    # Get all kernel json files
    jupyter_path = jupyter_runtime_dir()
    runtime_files = []
    for file_path in listdir(jupyter_path):
        full_path = join(jupyter_path, file_path)
        file_ext = splitext(file_path)[1]
        if (isfile(full_path) and file_ext == '.json'):
            runtime_files.append(file_path)

    # Get all the kernel ids
    kernel_ids = []
    for runtime_file in runtime_files:
        kernel_id = shorten_filename(runtime_file)
        if runtime_file.startswith('nbserver'): continue
        kernel_ids.append(kernel_id)

    # Sort
    def hex_sort(value):
        try: res = int('0x' + value, 16)
        except ValueError: res = 0
        return res
    kernel_ids.sort(key=hex_sort, reverse=True)

    # Return -> vim caller
    return kernel_ids


# -----------------------------------------------------------------------------
#        Parsers
# -----------------------------------------------------------------------------


def parse_iopub_for_reply(msgs, line_number):
    """Get kernel response from message pool (Async)
    Param: line_number: the message number of the corresponding code
    Use: some kernel (iperl) do not discriminate when clien ask user_expressions.
        But still they give a printable output
    """
    res = -1

    # Get _res from user expression
    try:
        # Requires the fix for https://github.com/JuliaLang/IJulia.jl/issues/815
        res = msgs['content']['user_expressions']['_res']['data']['text/plain']
    except (TypeError, KeyError): pass

    # Parse all execute
    for msg in msgs:
        try:
            # Get the result of execution
            # 1 content
            content = msg.get('content', False)
            if not content: continue

            # 2 execution _count
            ec = int(content.get('execution_count', 0))
            if not ec: continue
            if line_number not in (-1, ec): continue

            # 3 message type
            if msg['header']['msg_type'] not in ('execute_result', 'stream'): continue

            # 4 text
            if 'data' in content:
                res = content['data']['text/plain']
            else:
                # Jupyter bash style ...
                res = content['text']
            break
        except KeyError: pass
    return res


def parse_messages(section_info, msgs):
    """Message handler for Jupyter protocol (Async)

    Takes all messages on the I/O Public channel, including stdout, stderr,
    etc.
    Returns: a list of the formatted strings of their content.

    See also: <http://jupyter-client.readthedocs.io/en/stable/messaging.html>
    """
    # pylint: disable=too-many-branches
    # TODO section_info is not perfectly async
    # TODO remove complexity
    res = []
    for msg in msgs:
        s = ''
        default_count = section_info.cmd_count
        if 'msg_type' not in msg['header']:
            continue
        msg_type = msg['header']['msg_type']

        if msg_type == 'status':
            # I don't care status (idle or busy)
            continue

        if msg_type == 'stream':
            # Get data
            text = strip_color_escapes(msg['content']['text'])
            line_number = msg['content'].get('execution_count', default_count)
            # Set prompt
            if msg['content'].get('name', 'stdout') == 'stderr':
                prompt = 'SdE[{:d}]: '.format(line_number)
                dots = (' ' * (len(prompt.rstrip()) - 4)) + '...x '
            else:
                prompt = 'SdO[{:d}]: '.format(line_number)
                dots = (' ' * (len(prompt.rstrip()) - 4)) + '...< '
            s = prompt
            # Add continuation line, if necessary
            s += text.rstrip().replace('\n', '\n' + dots)
            # Set cmd_count: if it changed
            if line_number != default_count:
                section_info.set_cmd_count(line_number)

        elif msg_type == 'display_data':
            s += msg['content']['data']['text/plain']

        elif msg_type in ('execute_input', 'pyin'):
            line_number = msg['content'].get('execution_count', default_count)
            cmd = msg['content']['code']
            s = prettify_execute_intput(line_number, cmd, section_info.lang.prompt_in)
            # Set cmd_count: if it changed
            if line_number != default_count:
                section_info.set_cmd_count(line_number)

        elif msg_type in ('execute_result', 'pyout'):
            # Get the output
            line_number = msg['content'].get('execution_count', default_count)
            s = section_info.lang.prompt_out.format(line_number)
            s += msg['content']['data']['text/plain']
            # Set cmd_count: if it changed
            if line_number != default_count:
                section_info.set_cmd_count(line_number)

        elif msg_type in ('error', 'pyerr'):
            s = "\n".join(map(strip_color_escapes, msg['content']['traceback']))

        elif msg_type == 'input_request':
            section_info.vim.thread_echom('python input not supported in vim.', style='Error')
            continue  # unsure what to do here... maybe just return False?

        else:
            section_info.vim.thread_echom("Message type {} unrecognized!".format(msg_type))
            continue

        # List all messages
        res.append(s)

    return res
