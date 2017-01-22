# Copyright (c) 2009-2014 Upi Tamminen <desaster@gmail.com>
# See the COPYRIGHT file for more information

"""
This module contains ...
"""

import os
import re
import stat
import copy
import time

from twisted.python import log, failure
from twisted.internet import error

from cowrie.core import fs
from cowrie.core import shlex


class HoneyPotCommand(object):
    """
    """
    def __init__(self, protocol, *args):
        self.protocol = protocol
        self.args = list(args)
        self.environ = self.protocol.cmdstack[0].environ
        self.fs = self.protocol.fs
        self.data = None
        self.input_data = None
        self.write = self.protocol.pp.outReceived
        self.errorWrite = self.protocol.pp.errReceived
        # MS-DOS style redirect handling, inside the command
        # TODO: handle >>, 2>, etc
        if '>' in self.args:
            self.writtenBytes = 0
            self.write = self.write_to_file
            index = self.args.index(">")
            self.outfile = self.fs.resolve_path(str(self.args[(index + 1)]), self.protocol.cwd)
            del self.args[index:]
            self.safeoutfile = '%s/%s-%s-%s-redir_%s' % (
                self.protocol.cfg.get('honeypot', 'download_path'),
                time.strftime('%Y%m%d-%H%M%S'),
                self.protocol.getProtoTransport().transportId,
                self.protocol.terminal.transport.session.id,
                re.sub('[^A-Za-z0-9]', '_', self.outfile))
            perm = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
            try:
                self.fs.mkfile(self.outfile, 0, 0, 0, stat.S_IFREG | perm)
            except fs.FileNotFound:
                # The outfile locates at a non-existing directory.
                self.protocol.pp.outReceived('-bash: %s: No such file or directory\n' % self.outfile)
                self.write = self.write_to_failed
                self.outfile = None
                self.safeoutfile = None

            else:
                with open(self.safeoutfile, 'a'):
                    self.fs.update_realfile(self.fs.getfile(self.outfile), self.safeoutfile)


    def check_arguments(self, application, args):
        """
        """
        files = []
        for arg in args:
            path = self.fs.resolve_path(arg, self.protocol.cwd)
            if self.fs.isdir(path):
                self.errorWrite("{}: error reading `{}': Is a directory\n".format(application, arg))
                continue
            files.append(path)
        return files


    def set_input_data(self, data):
        """
        """
        self.input_data = data


    def write_to_file(self, data):
        """
        """
        with open(self.safeoutfile, 'a') as f:
            f.write(data)
        self.writtenBytes += len(data)
        self.fs.update_size(self.outfile, self.writtenBytes)


    def write_to_failed(self, data):
        """
        """
        pass

    def start(self):
        """
        """
        if self.write != self.write_to_failed:
            self.call()
        self.exit()


    def call(self):
        """
        """
        self.write('Hello World! [%s]\n' % (repr(self.args),))


    def exit(self):
        """
        Sometimes client is disconnected and command exits after. So cmdstack is gone
        """
        try:
            self.protocol.cmdstack.pop()
            self.protocol.cmdstack[-1].resume()
        except AttributeError:
            # Cmdstack could be gone already (wget + disconnect)
            pass


    def handle_CTRL_C(self):
        """
        """
        log.msg('Received CTRL-C, exiting..')
        self.write('^C\n')
        self.exit()


    def lineReceived(self, line):
        """
        """
        log.msg('QUEUED INPUT: {}'.format(line))
        # FIXME: naive command parsing, see lineReceived below
        self.protocol.cmdstack[0].cmdpending.append(shlex.split(line))


    def resume(self):
        """
        """
        pass


    def handle_TAB(self):
        """
        """
        pass


    def handle_CTRL_D(self):
        """
        """
        pass


    def __repr__(self):
        return str(self.__class__.__name__)



class HoneyPotShell(object):
    """
    """

    def __init__(self, protocol, interactive=True):
        self.protocol = protocol
        self.interactive = interactive
        self.cmdpending = []
        self.environ = protocol.environ
        self.lexer = None
        self.showPrompt()


    def lineReceived(self, line):
        """
        """
        log.msg(eventid='cowrie.command.input', input=line, format='CMD: %(input)s')
        self.lexer = shlex.shlex(instream=line, punctuation_chars=True)
        tokens = []
        while True:
            try:
                tok = self.lexer.get_token()
                # log.msg( "tok: %s" % (repr(tok)) )

                # Ignore parentheses
                tok_len = len(tok)
                tok = tok.strip('(')
                tok = tok.strip(')')
                if len(tok) != tok_len and tok == '':
                    continue

                if tok == self.lexer.eof:
                    if len(tokens):
                        self.cmdpending.append((tokens))
                        tokens = []
                    break
                # For now, execute all after &&
                elif tok == ';' or tok == '&&' or tok == '||':
                    if len(tokens):
                        self.cmdpending.append((tokens))
                        tokens = []
                        continue
                    else:
                        self.protocol.terminal.write(
                            '-bash: syntax error near unexpected token `{}\'\n'.format(tok))
                        break
                elif tok == '$?':
                    tok = "0"
                elif tok[0] == '$':
                    envRex = re.compile(r'^\$([_a-zA-Z0-9]+)$')
                    envSearch = envRex.search(tok)
                    if envSearch != None:
                        envMatch = envSearch.group(1)
                        if envMatch in list(self.environ.keys()):
                            tok = self.environ[envMatch]
                        else:
                            continue
                    envRex = re.compile(r'^\${([_a-zA-Z0-9]+)}$')
                    envSearch = envRex.search(tok)
                    if envSearch != None:
                        envMatch = envSearch.group(1)
                        if envMatch in list(self.environ.keys()):
                            tok = self.environ[envMatch]
                        else:
                            continue
                tokens.append(tok)
            except Exception as e:
                self.protocol.terminal.write(
                    'bash: syntax error: unexpected end of file\n')
                # Could run runCommand here, but i'll just clear the list instead
                log.msg( "exception: {}".format(e) )
                self.cmdpending = []
                self.showPrompt()
                return

        if len(self.cmdpending):
            self.runCommand()
        else:
            self.showPrompt()


    def runCommand(self):
        """
        """
        pp = None
        def runOrPrompt():
            if len(self.cmdpending):
                self.runCommand()
            elif self.interactive:
                self.showPrompt()
            else:
                ret = failure.Failure(error.ProcessDone(status=""))
                self.protocol.terminal.transport.processEnded(ret)

        def parsed_arguments(arguments):
            parsed_arguments = []
            for arg in arguments:
                parsed_arguments.append(arg)

            return parsed_arguments

        def parse_file_arguments(arguments):
            parsed_arguments = []
            for arg in arguments:
                matches = self.protocol.fs.resolve_path_wc(arg, self.protocol.cwd)
                if matches:
                    parsed_arguments.extend(matches)
                else:
                    parsed_arguments.append(arg)

            return parsed_arguments

        if not len(self.cmdpending):
            if self.interactive:
                self.showPrompt()
            else:
                ret = failure.Failure(error.ProcessDone(status=""))
                self.protocol.terminal.transport.processEnded(ret)
            return

        cmdAndArgs = self.cmdpending.pop(0)
        cmd2 = copy.copy(cmdAndArgs)

        # Probably no reason to be this comprehensive for just PATH...
        environ = copy.copy(self.environ)
        cmd_array = [ ]
        cmd = {}
        while len(cmdAndArgs):
            piece = cmdAndArgs.pop(0)
            if piece.count('='):
                key, value = piece.split('=', 1)
                environ[key] = value
                continue
            cmd['command'] = piece
            cmd['rargs'] = []
            break

        if not cmd['command']:
            runOrPrompt()
            return

        pipe_indices = [i for i, x in enumerate(cmdAndArgs) if x == "|"]
        multipleCmdArgs = []
        pipe_indices.append(len(cmdAndArgs))
        start = 0

        # Gather all arguments with pipes

        for index, pipe_indice in enumerate(pipe_indices):
            multipleCmdArgs.append(cmdAndArgs[start:pipe_indice])
            start = pipe_indice+1

        cmd['rargs'] = parse_file_arguments(multipleCmdArgs.pop(0))
        cmd_array.append(cmd)
        cmd = {}

        for index, value in enumerate(multipleCmdArgs):
            cmd['command'] = value.pop(0)
            cmd['rargs'] = parsed_arguments(value)
            cmd_array.append(cmd)
            cmd = {}

        lastpp = None
        exit = False
        for index, cmd in reversed(list(enumerate(cmd_array))):
            if cmd['command'] == "exit":
                exit = True

            cmdclass =  self.protocol.getCommand(cmd['command'], environ['PATH'] .split(':'))
            if cmdclass:
                log.msg(eventid='cowrie.command.success', input=cmd['command'] + " " + ' '.join(cmd['rargs']), format='Command found: %(input)s')
                if index == len(cmd_array)-1:
                    lastpp =  StdOutStdErrEmulationProtocol(self.protocol, cmdclass, cmd['rargs'], None, None)
                    pp = lastpp
                else:
                    pp = StdOutStdErrEmulationProtocol(self.protocol, cmdclass, cmd['rargs'], None, lastpp)
                    lastpp = pp
            else:
                log.msg(eventid='cowrie.command.failed',
                    input=' '.join(cmd2), format='Command not found: %(input)s')
                self.protocol.terminal.write('bash: %s: command not found\n' % (cmd['command'],))
                runOrPrompt()
        if pp:
            self.protocol.call_command(pp, cmdclass, *cmd_array[0]['rargs'])


    def resume(self):
        """
        """
        if self.interactive:
            self.protocol.setInsertMode()
        self.runCommand()


    def showPrompt(self):
        """
        """
        if not self.interactive:
            return

        cwd = self.protocol.cwd
        homelen = len(self.protocol.user.avatar.home)
        if cwd == self.protocol.user.avatar.home:
            cwd = '~'
        elif len(cwd) > (homelen+1) and \
                cwd[:(homelen+1)] == self.protocol.user.avatar.home + '/':
            cwd = '~' + cwd[homelen:]
        # Uncomment the three lines below for a 'better' CentOS look.
        # Rather than '[root@svr03 /var/log]#' is shows '[root@svr03 log]#'.
        #cwd = cwd.rsplit('/', 1)[-1]
        #if not cwd:
        #    cwd = '/'

        # Example: [root@svr03 ~]#   (More of a "CentOS" feel)
        # Example: root@svr03:~#     (More of a "Debian" feel)
        prompt = '{}@{}:{}'.format(self.protocol.user.username, self.protocol.hostname, cwd)
        if not self.protocol.user.uid:
            prompt += '# '    # "Root" user
        else:
            prompt += '$ '    # "Non-Root" user
        self.protocol.terminal.write(prompt)
        self.protocol.ps = (prompt , '> ')


    def eofReceived(self):
        """
        this should probably not go through ctrl-d, but use processprotocol to close stdin
        """
        log.msg("received eof, sending ctrl-d to command")
        if len(self.cmdstack):
            self.cmdstack[-1].handle_CTRL_D()


    def handle_CTRL_C(self):
        """
        """
        self.protocol.lineBuffer = []
        self.protocol.lineBufferIndex = 0
        self.protocol.terminal.write('\n')
        self.showPrompt()


    def handle_CTRL_D(self):
        """
        """
        log.msg('Received CTRL-D, exiting..')

        cmdclass =  self.protocol.commands['exit']
        pp = StdOutStdErrEmulationProtocol(self.protocol, cmdclass, None, None, None)
        self.protocol.call_command(pp, self.protocol.commands['exit'])


    def handle_TAB(self):
        """
        """
        if not len(self.protocol.lineBuffer):
            return
        l = ''.join(self.protocol.lineBuffer)
        if l[-1] == ' ':
            clue = ''
        else:
            clue = ''.join(self.protocol.lineBuffer).split()[-1]
        try:
            basedir = os.path.dirname(clue)
        except:
            pass
        if len(basedir) and basedir[-1] != '/':
            basedir += '/'

        files = []
        tmppath = basedir
        if not len(basedir):
            tmppath = self.protocol.cwd
        try:
            r = self.protocol.fs.resolve_path(tmppath, self.protocol.cwd)
        except:
            return
        for x in self.protocol.fs.get_path(r):
            if clue == '':
                files.append(x)
                continue
            if not x[fs.A_NAME].startswith(os.path.basename(clue)):
                continue
            files.append(x)

        if len(files) == 0:
            return

        # Clear early so we can call showPrompt if needed
        for i in range(self.protocol.lineBufferIndex):
            self.protocol.terminal.cursorBackward()
            self.protocol.terminal.deleteCharacter()

        newbuf = ''
        if len(files) == 1:
            newbuf = ' '.join(l.split()[:-1] + \
                ['%s%s' % (basedir, files[0][fs.A_NAME])])
            if files[0][fs.A_TYPE] == fs.T_DIR:
                newbuf += '/'
            else:
                newbuf += ' '
        else:
            if len(os.path.basename(clue)):
                prefix = os.path.commonprefix([x[fs.A_NAME] for x in files])
            else:
                prefix = ''
            first = l.split(' ')[:-1]
            newbuf = ' '.join(first + ['%s%s' % (basedir, prefix)])
            if newbuf == ''.join(self.protocol.lineBuffer):
                self.protocol.terminal.write('\n')
                maxlen = max([len(x[fs.A_NAME]) for x in files]) + 1
                perline = int(self.protocol.user.windowSize[1] / (maxlen + 1))
                count = 0
                for file in files:
                    if count == perline:
                        count = 0
                        self.protocol.terminal.write('\n')
                    self.protocol.terminal.write(file[fs.A_NAME].ljust(maxlen))
                    count += 1
                self.protocol.terminal.write('\n')
                self.showPrompt()

        self.protocol.lineBuffer = list(newbuf)
        self.protocol.lineBufferIndex = len(self.protocol.lineBuffer)
        self.protocol.terminal.write(newbuf)



class StdOutStdErrEmulationProtocol(object):
    """
    Pipe support written by Dave Germiquet
    """
    __author__ = 'davegermiquet'

    def __init__(self, protocol, cmd, cmdargs, input_data, next_command):
        self.cmd = cmd
        self.cmdargs = cmdargs
        self.input_data = input_data
        self.next_command = next_command
        self.data = ""
        self.err_data = ""
        self.protocol = protocol


    def connectionMade(self):
        """
        """
        self.input_data = None


    def outReceived(self, data):
        """
        """
        self.data = self.data + data

        if not self.next_command:
            if not self.protocol is None and not self.protocol.terminal is None:
                self.protocol.terminal.write(str(data))
            else:
                log.msg("Connection was probably lost. Could not write to terminal")


    def insert_command(self, command):
        """
        Insert the next command into the list.
        """
        command.next_command = self.next_command
        self.next_command = command


    def errReceived(self, data):
        """
        """
        self.protocol.terminal.write(data)
        self.err_data = self.err_data + data


    def inConnectionLost(self):
        """
        """
        pass


    def outConnectionLost(self):
        """
        """
        if self.next_command:
            self.next_command.input_data = self.data
            npcmd = self.next_command.cmd
            npcmdargs = self.next_command.cmdargs
            self.protocol.call_command(self.next_command, npcmd, *npcmdargs)


    def errConnectionLost(self):
        """
        """
        pass


    def processExited(self, reason):
        """
        """
        log.msg("processExited for %s, status %d" % (self.cmd, reason.value.exitCode))


    def processEnded(self, reason):
        """
        """
        log.msg("processEnded for %s, status %d" % (self.cmd, reason.value.exitCode))


