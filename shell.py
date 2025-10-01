#!/usr/bin/env python3
import os
import sys
import re

# Get shell prompt from PS1 environment variable or default
# @Return: prompt string
def get_prompt():
    ps1 = os.environ.get('PS1')
    if ps1 is None:
        return "$ "
    return ps1

# Find full path of executable command
# @Param cmd: command name
# @Return: full path or None if not found
def find_executable(cmd):
    # If contains a slash, treat as path
    if '/' in cmd:
        if os.access(cmd, os.X_OK) and os.path.isfile(cmd):
            return cmd
        return None
    path = os.environ.get('PATH', '')
    for d in path.split(os.pathsep):
        full = os.path.join(d, cmd)
        if os.access(full, os.X_OK) and os.path.isfile(full):
            return full
    return None

# Tokenize a command line string into tokens
# @Param s: command line string
# @Return: list of tokens
def tokenize(s):
    token_re = re.compile(r'''
        \s*(?:
            (>>)|                   # append redir
            ([^\s"']+)|             # unquoted token
            "((?:\\.|[^"])+)"|      # double-quoted
            '((?:\\.|[^'])+)'       # single-quoted
        )''', re.VERBOSE)
    pos = 0
    tokens = []
    while pos < len(s):
        m = token_re.match(s, pos)
        if not m:
            break
        if m.group(1):
            tokens.append(m.group(1))
        elif m.group(2):
            tokens.append(m.group(2))
        elif m.group(3):
            tokens.append(m.group(3).replace('\\"', '"'))
        elif m.group(4):
            tokens.append(m.group(4).replace("\\'", "'"))
        pos = m.end()
    return tokens

# Parse a command line into a list of commands with args and redirections
# Param line: command line string
# Return: list of command dicts: {'args':[], 'in':None, 'out':None, 'append':False}
def parse_pipeline(line):
    tokens = tokenize(line)
    if not tokens:
        return []
    # split by '|'
    segments = []
    cur = []
    for t in tokens:
        if t == '|':
            segments.append(cur)
            cur = []
        else:
            cur.append(t)
    segments.append(cur)

    cmds = []
    for seg in segments:
        infile = None
        outfile = None
        outfile_append = False
        args = []
        i = 0
        while i < len(seg):
            tok = seg[i]
            if tok == '>':
                i += 1
                if i < len(seg):
                    outfile = seg[i]
                else:
                    outfile = None
                i += 1
                continue
            elif tok == '>>':
                i += 1
                if i < len(seg):
                    outfile = seg[i]
                    outfile_append = True
                else:
                    outfile = None
                i += 1
                continue
            elif tok == '<':
                i += 1
                if i < len(seg):
                    infile = seg[i]
                else:
                    infile = None
                i += 1
                continue
            else:
                args.append(tok)
            i += 1
        if args:
            cmds.append({'args': args, 'in': infile, 'out': outfile, 'append': outfile_append})
    return cmds

# Execute a pipeline of commands
# @Param cmds: list of command dicts as from parse_pipeline
# @Param background: if True, do not wait for completion
# @Return: exit code of last command in pipeline (or 0 if background)
def run_pipeline(cmds, background):
    n = len(cmds)
    if n == 0:
        return 0

    pipes = []
    for i in range(n - 1):
        pipes.append(os.pipe())

    pids = []
    for i, cmd in enumerate(cmds):
        pid = os.fork()
        if pid == 0:
            # child
            # stdin
            if i == 0 and cmd['in']:
                try:
                    fdin = os.open(cmd['in'], os.O_RDONLY)
                    os.dup2(fdin, 0)
                    os.close(fdin)
                except Exception:
                    print(f"{cmd['in']}: command not found", file=sys.stderr)
                    os._exit(1)
            if i > 0:
                r = pipes[i - 1][0]
                os.dup2(r, 0)

            # stdout
            if i == n - 1 and cmd['out']:
                try:
                    flags = os.O_WRONLY | os.O_CREAT
                    if cmd.get('append'):
                        flags |= os.O_APPEND
                    else:
                        flags |= os.O_TRUNC
                    fdout = os.open(cmd['out'], flags, 0o666)
                    os.dup2(fdout, 1)
                    os.close(fdout)
                except Exception:
                    print(f"{cmd['out']}: command not found", file=sys.stderr)
                    os._exit(1)
            if i < n - 1:
                w = pipes[i][1]
                os.dup2(w, 1)

            # close all pipe fds in child
            for (r, w) in pipes:
                try:
                    os.close(r)
                except Exception:
                    pass
                try:
                    os.close(w)
                except Exception:
                    pass

            # execute
            prog = cmd['args'][0]
            full = find_executable(prog)
            if full is None:
                print(f"{prog}: command not found", file=sys.stderr)
                os._exit(1)
            try:
                os.execve(full, cmd['args'], os.environ)
            except Exception:
                print(f"{prog}: command not found", file=sys.stderr)
                os._exit(1)
        else:
            pids.append(pid)

    # parent: close pipe fds
    for (r, w) in pipes:
        try:
            os.close(r)
        except Exception:
            pass
        try:
            os.close(w)
        except Exception:
            pass

    exit_codes = []
    if background:
        # don't wait for children now
        return 0
    else:
        for pid in pids:
            try:
                _, status = os.waitpid(pid, 0)
                if os.WIFEXITED(status):
                    exit_codes.append(os.WEXITSTATUS(status))
                else:
                    exit_codes.append(1)
            except Exception:
                exit_codes.append(1)

    # return last exit code
    return exit_codes[-1] if exit_codes else 0

# Main loop of the shell
def main():
    prompt = get_prompt()
    # interactive if stdin is a tty
    interactive = sys.stdin.isatty()

    while True:
        try:
            if interactive and prompt:
                # print prompt to stdout and flush
                sys.stdout.write(prompt)
                sys.stdout.flush()
            line = sys.stdin.readline()
            if not line:  # EOF
                break
            line = line.strip()
            if not line:
                continue

            # background if ends with &
            background = False
            if line.endswith('&'):
                background = True
                line = line[:-1].strip()

            # parse pipeline and redirections
            cmds = parse_pipeline(line)
            if not cmds:
                continue

            # handle builtins cd and exit when single command and no pipes
            if len(cmds) == 1:
                args = cmds[0]['args']
                if args[0] == 'exit':
                    sys.exit(0)
                if args[0] == 'cd':
                    # change directory in parent
                    target = args[1] if len(args) > 1 else os.environ.get('HOME', '/')
                    try:
                        os.chdir(target)
                    except Exception as e:
                        print(str(e), file=sys.stderr)
                    continue

            status = run_pipeline(cmds, background)
            if status != 0:
                print(f"Program terminated with exit code {status}.", file=sys.stderr)

        except KeyboardInterrupt:
            # print newline and continue
            sys.stdout.write('\n')
            continue
        except EOFError:
            break


if __name__ == '__main__':
    main()
