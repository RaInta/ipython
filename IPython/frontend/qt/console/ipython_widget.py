""" A FrontendWidget that emulates the interface of the console IPython and
    supports the additional functionality provided by the IPython kernel.

    TODO: Add support for retrieving the system default editor. Requires code
          paths for Windows (use the registry), Mac OS (use LaunchServices), and
          Linux (use the xdg system).
"""

# Standard library imports
from collections import namedtuple
from subprocess import Popen

# System library imports
from PyQt4 import QtCore, QtGui

# Local imports
from IPython.core.inputsplitter import IPythonInputSplitter
from IPython.core.usage import default_banner
from IPython.utils.traitlets import Bool, Str
from frontend_widget import FrontendWidget

# The default style sheet: black text on a white background.
default_style_sheet = '''
    .error { color: red; }
    .in-prompt { color: navy; }
    .in-prompt-number { font-weight: bold; }
    .out-prompt { color: darkred; }
    .out-prompt-number { font-weight: bold; }
'''
default_syntax_style = 'default'

# A dark style sheet: white text on a black background.
dark_style_sheet = '''
    QPlainTextEdit, QTextEdit { background-color: black; color: white }
    QFrame { border: 1px solid grey; }
    .error { color: red; }
    .in-prompt { color: lime; }
    .in-prompt-number { color: lime; font-weight: bold; }
    .out-prompt { color: red; }
    .out-prompt-number { color: red; font-weight: bold; }
'''
dark_syntax_style = 'monokai'

# Default prompts.
default_in_prompt = 'In [<span class="in-prompt-number">%i</span>]: '
default_out_prompt = 'Out[<span class="out-prompt-number">%i</span>]: '


class IPythonWidget(FrontendWidget):
    """ A FrontendWidget for an IPython kernel.
    """

    # If set, the 'custom_edit_requested(str, int)' signal will be emitted when
    # an editor is needed for a file. This overrides 'editor' and 'editor_line'
    # settings.
    custom_edit = Bool(False)
    custom_edit_requested = QtCore.pyqtSignal(object, object)

    # A command for invoking a system text editor. If the string contains a
    # {filename} format specifier, it will be used. Otherwise, the filename will
    # be appended to the end the command.
    editor = Str('default', config=True)

    # The editor command to use when a specific line number is requested. The
    # string should contain two format specifiers: {line} and {filename}. If
    # this parameter is not specified, the line number option to the %edit magic
    # will be ignored.
    editor_line = Str(config=True)

    # A CSS stylesheet. The stylesheet can contain classes for:
    #     1. Qt: QPlainTextEdit, QFrame, QWidget, etc
    #     2. Pygments: .c, .k, .o, etc (see PygmentsHighlighter)
    #     3. IPython: .error, .in-prompt, .out-prompt, etc
    style_sheet = Str(default_style_sheet, config=True)
    
    # If not empty, use this Pygments style for syntax highlighting. Otherwise,
    # the style sheet is queried for Pygments style information.
    syntax_style = Str(default_syntax_style, config=True)

    # Prompts.
    in_prompt = Str(default_in_prompt, config=True)
    out_prompt = Str(default_out_prompt, config=True)

    # FrontendWidget protected class variables.
    _input_splitter_class = IPythonInputSplitter

    # IPythonWidget protected class variables.
    _PromptBlock = namedtuple('_PromptBlock', ['block', 'length', 'number'])
    _payload_source_edit = 'IPython.zmq.zmqshell.ZMQInteractiveShell.edit_magic'
    _payload_source_page = 'IPython.zmq.page.page'

    #---------------------------------------------------------------------------
    # 'object' interface
    #---------------------------------------------------------------------------
    
    def __init__(self, *args, **kw):
        super(IPythonWidget, self).__init__(*args, **kw)

        # IPythonWidget protected variables.
        self._previous_prompt_obj = None

        # Initialize widget styling.
        self._style_sheet_changed()
        self._syntax_style_changed()

    #---------------------------------------------------------------------------
    # 'BaseFrontendMixin' abstract interface
    #---------------------------------------------------------------------------

    def _handle_complete_reply(self, rep):
        """ Reimplemented to support IPython's improved completion machinery.
        """
        cursor = self._get_cursor()
        if rep['parent_header']['msg_id'] == self._complete_id and \
                cursor.position() == self._complete_pos:
            # The completer tells us what text was actually used for the
            # matching, so we must move that many characters left to apply the
            # completions.
            text = rep['content']['matched_text']
            cursor.movePosition(QtGui.QTextCursor.Left, n=len(text))
            self._complete_with_items(cursor, rep['content']['matches'])

    def _handle_history_reply(self, msg):
        """ Implemented to handle history replies, which are only supported by
            the IPython kernel.
        """
        history_dict = msg['content']['history']
        items = [ history_dict[key] for key in sorted(history_dict.keys()) ]
        self._set_history(items)

    def _handle_prompt_reply(self, msg):
        """ Implemented to handle prompt number replies, which are only
            supported by the IPython kernel.
        """
        content = msg['content']
        self._show_interpreter_prompt(content['prompt_number'], 
                                      content['input_sep'])

    def _handle_pyout(self, msg):
        """ Reimplemented for IPython-style "display hook".
        """
        if not self._hidden and self._is_from_this_session(msg):
            content = msg['content']
            prompt_number = content['prompt_number']
            self._append_plain_text(content['output_sep'])
            self._append_html(self._make_out_prompt(prompt_number))
            self._append_plain_text(content['data'] + '\n' + 
                                    content['output_sep2'])

    def _started_channels(self):
        """ Reimplemented to make a history request.
        """
        super(IPythonWidget, self)._started_channels()
        # FIXME: Disabled until history requests are properly implemented.
        #self.kernel_manager.xreq_channel.history(raw=True, output=False)

    def _handle_kernel_died(self, since_last_heartbeat):
        """ Handle the kernel's death by asking if the user wants to restart.
        """
        message = 'The kernel heartbeat has been inactive for %.2f ' \
                  'seconds. Do you want to restart the kernel? You may ' \
                  'first want to check the network connection.' % since_last_heartbeat
        self._kernel_restart(message)

    #---------------------------------------------------------------------------
    # 'FrontendWidget' interface
    #---------------------------------------------------------------------------

    def execute_file(self, path, hidden=False):
        """ Reimplemented to use the 'run' magic.
        """
        self.execute('%%run %s' % path, hidden=hidden)

    #---------------------------------------------------------------------------
    # 'FrontendWidget' protected interface
    #---------------------------------------------------------------------------

    def _complete(self):
        """ Reimplemented to support IPython's improved completion machinery.
        """
        # We let the kernel split the input line, so we *always* send an empty
        # text field. Readline-based frontends do get a real text field which
        # they can use.
        text = ''
        
        # Send the completion request to the kernel
        self._complete_id = self.kernel_manager.xreq_channel.complete(
            text,                                    # text
            self._get_input_buffer_cursor_line(),    # line
            self._get_input_buffer_cursor_column(),  # cursor_pos
            self.input_buffer)                       # block 
        self._complete_pos = self._get_cursor().position()

    def _get_banner(self):
        """ Reimplemented to return IPython's default banner.
        """
        return default_banner + '\n'

    def _process_execute_error(self, msg):
        """ Reimplemented for IPython-style traceback formatting.
        """
        content = msg['content']
        traceback = '\n'.join(content['traceback']) + '\n'
        if False:
            # FIXME: For now, tracebacks come as plain text, so we can't use
            # the html renderer yet.  Once we refactor ultratb to produce
            # properly styled tracebacks, this branch should be the default
            traceback = traceback.replace(' ', '&nbsp;')
            traceback = traceback.replace('\n', '<br/>')

            ename = content['ename']
            ename_styled = '<span class="error">%s</span>' % ename
            traceback = traceback.replace(ename, ename_styled)

            self._append_html(traceback)
        else:
            # This is the fallback for now, using plain text with ansi escapes
            self._append_plain_text(traceback)

    def _process_execute_payload(self, item):
        """ Reimplemented to handle %edit and paging payloads.
        """
        if item['source'] == self._payload_source_edit:
            self._edit(item['filename'], item['line_number'])
            return True
        elif item['source'] == self._payload_source_page:
            self._page(item['data'])
            return True
        else:
            return False

    def _show_interpreter_prompt(self, number=None, input_sep='\n'):
        """ Reimplemented for IPython-style prompts.
        """
        # If a number was not specified, make a prompt number request.
        if number is None:
            self.kernel_manager.xreq_channel.prompt()
            return

        # Show a new prompt and save information about it so that it can be
        # updated later if the prompt number turns out to be wrong.
        self._prompt_sep = input_sep
        self._show_prompt(self._make_in_prompt(number), html=True)
        block = self._control.document().lastBlock()
        length = len(self._prompt)
        self._previous_prompt_obj = self._PromptBlock(block, length, number)

        # Update continuation prompt to reflect (possibly) new prompt length.
        self._set_continuation_prompt(
            self._make_continuation_prompt(self._prompt), html=True)

    def _show_interpreter_prompt_for_reply(self, msg):
        """ Reimplemented for IPython-style prompts.
        """
        # Update the old prompt number if necessary.
        content = msg['content']
        previous_prompt_number = content['prompt_number']
        if self._previous_prompt_obj and \
                self._previous_prompt_obj.number != previous_prompt_number:
            block = self._previous_prompt_obj.block

            # Make sure the prompt block has not been erased.
            if block.isValid() and not block.text().isEmpty():

                # Remove the old prompt and insert a new prompt.
                cursor = QtGui.QTextCursor(block)
                cursor.movePosition(QtGui.QTextCursor.Right,
                                    QtGui.QTextCursor.KeepAnchor, 
                                    self._previous_prompt_obj.length)
                prompt = self._make_in_prompt(previous_prompt_number)
                self._prompt = self._insert_html_fetching_plain_text(
                    cursor, prompt)

                # When the HTML is inserted, Qt blows away the syntax
                # highlighting for the line, so we need to rehighlight it.
                self._highlighter.rehighlightBlock(cursor.block())

            self._previous_prompt_obj = None

        # Show a new prompt with the kernel's estimated prompt number.
        next_prompt = content['next_prompt']
        self._show_interpreter_prompt(next_prompt['prompt_number'], 
                                      next_prompt['input_sep'])

    #---------------------------------------------------------------------------
    # 'IPythonWidget' protected interface
    #---------------------------------------------------------------------------

    def _edit(self, filename, line=None):
        """ Opens a Python script for editing.

        Parameters:
        -----------
        filename : str
            A path to a local system file.

        line : int, optional
            A line of interest in the file.
        """
        if self.custom_edit:
            self.custom_edit_requested.emit(filename, line)
        elif self.editor == 'default':
            self._append_plain_text('No default editor available.\n')
        else:
            try:
                filename = '"%s"' % filename
                if line and self.editor_line:
                    command = self.editor_line.format(filename=filename,
                                                      line=line)
                else:
                    try:
                        command = self.editor.format()
                    except KeyError:
                        command = self.editor.format(filename=filename)
                    else:
                        command += ' ' + filename
            except KeyError:
                self._append_plain_text('Invalid editor command.\n')
            else:
                try:
                    Popen(command, shell=True)
                except OSError:
                    msg = 'Opening editor with command "%s" failed.\n'
                    self._append_plain_text(msg % command)

    def _make_in_prompt(self, number):
        """ Given a prompt number, returns an HTML In prompt.
        """
        body = self.in_prompt % number
        return '<span class="in-prompt">%s</span>' % body

    def _make_continuation_prompt(self, prompt):
        """ Given a plain text version of an In prompt, returns an HTML
            continuation prompt.
        """
        end_chars = '...: '
        space_count = len(prompt.lstrip('\n')) - len(end_chars)
        body = '&nbsp;' * space_count + end_chars
        return '<span class="in-prompt">%s</span>' % body
        
    def _make_out_prompt(self, number):
        """ Given a prompt number, returns an HTML Out prompt.
        """
        body = self.out_prompt % number
        return '<span class="out-prompt">%s</span>' % body

    #------ Trait change handlers ---------------------------------------------

    def _style_sheet_changed(self):
        """ Set the style sheets of the underlying widgets.
        """
        self.setStyleSheet(self.style_sheet)
        self._control.document().setDefaultStyleSheet(self.style_sheet)
        if self._page_control:
            self._page_control.document().setDefaultStyleSheet(self.style_sheet)

        bg_color = self._control.palette().background().color()
        self._ansi_processor.set_background_color(bg_color)

    def _syntax_style_changed(self):
        """ Set the style for the syntax highlighter.
        """
        if self.syntax_style:
            self._highlighter.set_style(self.syntax_style)
        else:
            self._highlighter.set_style_sheet(self.style_sheet)
        
