import os
import re
import subprocess

from lxml import etree
from collections import defaultdict

from hotdoc.core.symbols import *
from hotdoc.core.comment_block import Comment, comment_from_tag
from hotdoc.core.base_extension import BaseExtension, ExtDependency
from hotdoc.core.base_formatter import Formatter
from hotdoc.core.file_includer import find_md_file
from hotdoc.core.links import Link
from hotdoc.core.doc_tree import Page
from hotdoc.core.wizard import HotdocWizard

from hotdoc.parsers.gtk_doc_parser import GtkDocStringFormatter

from hotdoc.utils.wizard import Skip
from hotdoc.utils.patcher import Patcher
from hotdoc.utils.utils import get_all_extension_classes

from .gi_html_formatter import GIHtmlFormatter
from .transition_scripts.sgml_to_sections import parse_sections, convert_to_markdown

# FIXME: might conflict with comment_block.Annotation
class Annotation (object):
    def __init__(self, nick, help_text, value=None):
        self.nick = nick
        self.help_text = help_text
        self.value = value

class Flag (object):
    def __init__ (self, nick, link):
        self.nick = nick
        self.link = link

# FIXME: is that subclassing really helpful ?
class RunLastFlag (Flag):
    def __init__(self):
        Flag.__init__ (self, "Run Last",
                "https://developer.gnome.org/gobject/unstable/gobject-Signals.html#G-SIGNAL-RUN-LAST:CAPS")


class RunFirstFlag (Flag):
    def __init__(self):
        Flag.__init__ (self, "Run First",
                "https://developer.gnome.org/gobject/unstable/gobject-Signals.html#G-SIGNAL-RUN-FIRST:CAPS")


class RunCleanupFlag (Flag):
    def __init__(self):
        Flag.__init__ (self, "Run Cleanup",
                "https://developer.gnome.org/gobject/unstable/gobject-Signals.html#G-SIGNAL-RUN-CLEANUP:CAPS")


class NoHooksFlag (Flag):
    def __init__(self):
        Flag.__init__(self, "No Hooks",
"https://developer.gnome.org/gobject/unstable/gobject-Signals.html#G-SIGNAL-NO-HOOKS:CAPS")


class WritableFlag (Flag):
    def __init__(self):
        Flag.__init__ (self, "Write", None)


class ReadableFlag (Flag):
    def __init__(self):
        Flag.__init__ (self, "Read", None)


class ConstructFlag (Flag):
    def __init__(self):
        Flag.__init__ (self, "Construct", None)


class ConstructOnlyFlag (Flag):
    def __init__(self):
        Flag.__init__ (self, "Construct Only", None)

ALLOW_NONE_HELP = \
"NULL is OK, both for passing and returning"

TRANSFER_NONE_HELP = \
"Don't free data after the code is done"

TRANSFER_FULL_HELP = \
"Free data after the code is done"

TRANSFER_FLOATING_HELP = \
"Alias for transfer none, used for objects with floating refs"

TRANSFER_CONTAINER_HELP = \
"Free data container after the code is done"

CLOSURE_HELP = \
"This parameter is a closure for callbacks, many bindings can pass NULL to %s"

CLOSURE_DATA_HELP = \
"This parameter is a closure for callbacks, many bindings can pass NULL here"

DIRECTION_OUT_HELP = \
"Parameter for returning results"

DIRECTION_INOUT_HELP = \
"Parameter for input and for returning results"

DIRECTION_IN_HELP = \
"Parameter for input. Default is transfer none"

ARRAY_HELP = \
"Parameter points to an array of items"

ELEMENT_TYPE_HELP = \
"Generic and defining element of containers and arrays"

SCOPE_ASYNC_HELP = \
"The callback is valid until first called"

SCOPE_CALL_HELP = \
"The callback is valid only during the call to the method"

SCOPE_NOTIFIED_HELP=\
"The callback is valid until the GDestroyNotify argument is called"

NULLABLE_HELP = \
"NULL may be passed to the value"

DEFAULT_HELP = \
"Default parameter value (for in case the shadows-to function has less parameters)"

# VERY DIFFERENT FROM THE PREVIOUS ONE BEWARE :P
OPTIONAL_HELP = \
"NULL may be passed instead of a pointer to a location"

# WTF
TYPE_HELP = \
"Override the parsed C type with given type"

class GIInfo(object):
    def __init__(self, node, parent_name):
        self.node = node
        self.parent_name = re.sub('\.', '', parent_name)

class GIClassInfo(GIInfo):
    def __init__(self, node, parent_name, class_struct_name, is_interface):
        GIInfo.__init__(self, node, parent_name)
        self.class_struct_name = class_struct_name
        self.vmethods = {}
        self.signals = {}
        self.properties = {}
        self.is_interface = is_interface

# FIXME: this code is quite a mess
class GIRParser(object):
    def __init__(self, doc_repo, gir_file):
        self.namespace = None
        self.identifier_prefix = None
        self.gir_class_infos = {}
        self.gir_callable_infos = {}
        self.python_names = {}
        self.c_names = {}
        self.javascript_names = {}
        self.unintrospectable_symbols = {}
        self.gir_children_map = {}
        self.gir_hierarchies = {}
        self.gir_types = {}
        self.global_hierarchy = None
        self.doc_repo = doc_repo
        self.nsmap = {}

        self.parsed_files = []

        self.gir_class_map = {}

        self.__parse_gir_file (gir_file)
        self.__create_hierarchies()

    def __create_hierarchies(self):
        for gi_name, klass in self.gir_types.iteritems():
            hierarchy = self.__create_hierarchy (klass)
            self.gir_hierarchies[gi_name] = hierarchy

        hierarchy = []
        for c_name, klass in self.gir_class_infos.iteritems():
            if klass.parent_name != self.namespace:
                continue
            if not klass.node.tag.endswith (('class', 'interface')):
                continue

            gi_name = '%s.%s' % (klass.parent_name, klass.node.attrib['name'])
            klass_name = self.__get_klass_name (klass.node)
            link = Link(None, klass_name, klass_name)
            symbol = QualifiedSymbol(type_tokens=[link])
            parents = reversed(self.gir_hierarchies[gi_name])
            for parent in parents:
                hierarchy.append ((parent, symbol))
                symbol = parent

        self.global_hierarchy = hierarchy

    def __get_klass_name(self, klass):
        klass_name = klass.attrib.get('{%s}type' % self.nsmap['c'])
        if not klass_name:
            klass_name = klass.attrib.get('{%s}type-name' % self.nsmap['glib'])
        return klass_name

    def __create_hierarchy (self, klass):
        klaass = klass
        hierarchy = []
        while (True):
            parent_name = klass.attrib.get('parent')
            if not parent_name:
                break

            if not '.' in parent_name:
                namespace = klass.getparent().attrib['name']
                parent_name = '%s.%s' % (namespace, parent_name)
            parent_class = self.gir_types[parent_name]
            children = self.gir_children_map.get(parent_name)
            klass_name = self.__get_klass_name (klass)

            if not klass_name in children:
                link = Link(None, klass_name, klass_name)
                sym = QualifiedSymbol(type_tokens=[link])
                children[klass_name] = sym

            klass_name = self.__get_klass_name(parent_class)
            link = Link(None, klass_name, klass_name)
            sym = QualifiedSymbol(type_tokens=[link])
            hierarchy.append (sym)

            klass = parent_class

        hierarchy.reverse()
        return hierarchy

    def __find_gir_file(self, gir_name):
        xdg_dirs = os.getenv('XDG_DATA_DIRS') or ''
        xdg_dirs = [p for p in xdg_dirs.split(':') if p]
        xdg_dirs.append(self.doc_repo.datadir)
        for dir_ in xdg_dirs:
            gir_file = os.path.join(dir_, 'gir-1.0', gir_name)
            if os.path.exists(gir_file):
                return gir_file
        return None

    def __parse_gir_file (self, gir_file):
        if gir_file in self.parsed_files:
            return

        self.parsed_files.append (gir_file)

        tree = etree.parse (gir_file)
        root = tree.getroot()

        if self.namespace is None:
            ns = root.find("{http://www.gtk.org/introspection/core/1.0}namespace")
            self.namespace = ns.attrib['name']
            self.identifier_prefix = ns.attrib['{http://www.gtk.org/introspection/c/1.0}identifier-prefixes']

        self.nsmap.update({k:v for k,v in root.nsmap.iteritems() if k})
        for child in root:
            if child.tag == "{http://www.gtk.org/introspection/core/1.0}namespace":
                self.__parse_namespace(self.nsmap, child)
            elif child.tag == "{http://www.gtk.org/introspection/core/1.0}include":
                inc_name = child.attrib["name"]
                inc_version = child.attrib["version"]
                gir_file = self.__find_gir_file('%s-%s.gir' % (inc_name,
                    inc_version))
                self.__parse_gir_file (gir_file)

    def __parse_namespace (self, nsmap, ns):
        ns_name = ns.attrib["name"]

        for child in ns:
            if child.tag == "{http://www.gtk.org/introspection/core/1.0}class":
                self.__parse_gir_record(nsmap, ns_name, child, is_class=True)
            elif child.tag == "{http://www.gtk.org/introspection/core/1.0}interface":
                self.__parse_gir_record(nsmap, ns_name, child, is_interface=True)
            elif child.tag == "{http://www.gtk.org/introspection/core/1.0}record":
                self.__parse_gir_record(nsmap, ns_name, child)
            elif child.tag == "{http://www.gtk.org/introspection/core/1.0}callback":
                self.__parse_gir_callback (nsmap, ns_name, child)
            elif child.tag == "{http://www.gtk.org/introspection/core/1.0}enumeration":
                self.__parse_gir_enum (nsmap, ns_name, child)
            elif child.tag == "{http://www.gtk.org/introspection/core/1.0}bitfield":
                self.__parse_gir_enum (nsmap, ns_name, child)
            elif child.tag == "{http://www.gtk.org/introspection/core/1.0}constant":
                self.__parse_gir_constant (nsmap, ns_name, child)
            elif child.tag == "{http://www.gtk.org/introspection/core/1.0}function":
                self.__parse_gir_function (nsmap, ns_name, child)

    def __parse_gir_record (self, nsmap, ns_name, klass, is_interface=False,
            is_class=False):
        name = '%s.%s' % (ns_name, klass.attrib["name"])
        self.gir_types[name] = klass
        self.gir_children_map[name] = {}
        c_name = klass.attrib.get('{%s}type' % nsmap['c'])
        if not c_name:
            return

        class_struct_name = klass.attrib.get('{http://www.gtk.org/introspection/glib/1.0}type-struct') 

        gi_class_info = GIClassInfo (klass, ns_name, '%s%s' % (ns_name,
            class_struct_name), is_interface)

        if class_struct_name:
            self.gir_class_map['%s%s' % (ns_name, class_struct_name)] = gi_class_info

        if is_class or is_interface:
            self.gir_class_infos[c_name] = gi_class_info
            class_name = '%s::%s' % (c_name, c_name)

            self.c_names[class_name] = c_name
            self.python_names[class_name] = name
            self.javascript_names[class_name] = name

        self.c_names[c_name] = c_name
        self.python_names[c_name] = name
        self.javascript_names[c_name] = name


        for child in klass:
            if child.tag == "{http://www.gtk.org/introspection/core/1.0}method":
                child_cname = self.__parse_gir_function (nsmap, name, child,
                        is_method=True)
            elif child.tag == "{http://www.gtk.org/introspection/core/1.0}function":
                child_cname = self.__parse_gir_function (nsmap, name, child)
            elif child.tag == "{http://www.gtk.org/introspection/core/1.0}constructor":
                child_cname = self.__parse_gir_function (nsmap, name, child,
                        is_constructor=True)
            elif child.tag == "{http://www.gtk.org/introspection/glib/1.0}signal":
                child_cname = self.__parse_gir_signal (nsmap, c_name, child)
                gi_class_info.signals[child_cname] = child
            elif child.tag == "{http://www.gtk.org/introspection/core/1.0}property":
                self.__parse_gir_property (nsmap, c_name, child)
                gi_class_info.properties[child.attrib['name']] = child
            elif child.tag == "{http://www.gtk.org/introspection/core/1.0}virtual-method":
                child_cname = self.__parse_gir_vmethod (nsmap, c_name, child)
                gi_class_info.vmethods[child_cname] = child

    def __parse_gir_callable_common (self, callable_, c_id, c_name, python_name,
            js_name, class_name, is_method=False, is_constructor=False):
        introspectable = callable_.attrib.get('introspectable')

        if introspectable == '0':
            self.unintrospectable_symbols[c_id] = True

        self.c_names[c_id] = c_name
        self.python_names[c_id] = python_name
        self.javascript_names[c_id] = js_name

        info = GIInfo (callable_, class_name)
        self.gir_callable_infos[c_id] = info

    def __parse_gir_vmethod (self, nsmap, class_name, vmethod):
        name = vmethod.attrib['name']
        c_id = "%s:::%s---%s" % (class_name, name, 'vfunc')
        self.__parse_gir_callable_common (vmethod, c_id, name, name, name,
                class_name)
        return name

    def __parse_gir_signal (self, nsmap, class_name, signal):
        name = signal.attrib["name"]
        c_id = "%s:::%s---%s" % (class_name, name, 'signal')
        self.__parse_gir_callable_common (signal, c_id, name, name, name, class_name)
        return name

    def __parse_gir_property (self, nsmap, class_name, prop):
        name = prop.attrib["name"]
        c_name = "%s:::%s---%s" % (class_name, name, 'property')

    def __parse_gir_function (self, nsmap, class_name, function,
            is_method=False, is_constructor=False):
        python_name = '%s.%s' % (class_name, function.attrib['name'])
        js_name = '%s.prototype.%s' % (class_name, function.attrib['name'])
        c_name = function.attrib['{%s}identifier' % nsmap['c']]
        self.__parse_gir_callable_common (function, c_name, c_name, python_name,
                js_name, class_name, is_method=is_method,
                is_constructor=is_constructor)
        return c_name

    def __parse_gir_callback (self, nsmap, class_name, function):
        name = '%s.%s' % (class_name, function.attrib['name'])
        c_name = function.attrib['{%s}type' % nsmap['c']]
        self.gir_types[name] = function
        self.__parse_gir_callable_common (function, c_name, c_name, name, name,
                class_name)
        return c_name

    def __parse_gir_constant (self, nsmap, class_name, constant):
        name = '%s.%s' % (class_name, constant.attrib['name'])
        c_name = constant.attrib['{%s}type' % nsmap['c']]
        self.c_names[c_name] = c_name
        self.python_names[c_name] = name
        self.javascript_names[c_name] = name

    def __parse_gir_enum (self, nsmap, class_name, enum):
        name = '%s.%s' % (class_name, enum.attrib['name'])
        self.gir_types[name] = enum
        c_name = enum.attrib['{%s}type' % nsmap['c']]
        self.c_names[c_name] = c_name
        self.python_names[c_name] = name
        self.javascript_names[c_name] = name
        for c in enum:
            if c.tag == "{http://www.gtk.org/introspection/core/1.0}member":
                m_name = '%s.%s' % (name, c.attrib["name"].upper())
                c_name = c.attrib['{%s}identifier' % nsmap['c']]
                self.c_names[c_name] = c_name
                self.python_names[c_name] = m_name
                self.javascript_names[c_name] = m_name

    def __get_gir_type (self, name):
        namespaced = '%s.%s' % (self.namespace, name)
        klass = self.gir_types.get (namespaced)
        if klass is not None:
            return klass
        return self.gir_types.get (name)

    def type_tokens_from_gitype (self, ptype_name):
        qs = None

        if ptype_name == 'none':
            return None

        gitype = self.__get_gir_type (ptype_name)
        if gitype is not None:
            c_type = gitype.attrib['{http://www.gtk.org/introspection/c/1.0}type']
            ptype_name = c_type

        type_link = Link (None, ptype_name, ptype_name)

        tokens = [type_link]
        tokens += '*'

        return tokens

DESCRIPTION=\
"""
Parse a gir file and add signals, properties, classes
and virtual methods.

Can output documentation for various
languages.

Must be used in combination with the C extension.
"""

PROMPT_GTK_PORT_MAIN=\
"""
Porting from gtk-doc is a bit involved, and you will
want to manually go over generated markdown files to
improve pandoc's conversion (or contribute patches
to pandoc's docbook reader if you know haskell. I don't).

You'll want to make sure you have built the documentation
with gtk-doc first, it should be easy as checking that
there is an xml directory in the old documentation folder.

If not, you'll want to verify you have run make,
and possibly run ./configure --enable-gtk-doc in the
root directory beforehand.

Press Enter once you made sure you're all set. """

PROMPT_SECTIONS_FILE=\
"""
Good.

The first thing this conversion tool will need is the
path to the "sections" file. It usually is located in
the project's documentation folder, with a name such as
'$(project_name)-sections.txt'.

Path to the sections file ? """

PROMPT_SECTIONS_CONVERSION=\
"""
Thanks, I don't know what I would do without you.

Probably just sit there idling.

Anyway, the next step is to go over certain comments
in the source code and either rename them or place
them in the markdown files.

These comments are the "SECTION" comments, which
either document classes and should stay in the source
code, or were generic comments that have nothing to do
in the source code and belong in the standalone markdown
pages.

FYI, I have found %d section comments and %d class comments.

Don't worry, I can do that for you, I'll just need
your permission to slightly modify the source files.

Permission granted [y,n]? """

PROMPT_COMMIT=\
"""
Sweet.

Should I commit the files I modified [y,n]? """

PROMPT_DESTINATION=\
"""
Nice.

We can now finalize the port, by generating the standalone
markdown pages that will form the skeleton of your documentation.

I'll need you to provide me with a directory in which
to output these files (markdown_files seems like a pretty sane
choice but feel free to go wild).

If the directory does not exist it will be created.

Where should I write the markdown pages ? """

PROMPT_SGML_FILE=\
"""
I'll also need the path to the sgml file, it should look
something like $(project_name)-docs.sgml

Path to the SGML file ? """

def get_section_comments(wizard):
    gir_file = wizard.config.get('gir_file')
    if not os.path.exists(gir_file):
        gir_file = wizard.resolve_config_path(gir_file)

    root = etree.parse(gir_file).getroot()
    xns = root.find("{http://www.gtk.org/introspection/core/1.0}namespace")
    ns = xns.attrib['name']
    xclasses = root.findall('.//{http://www.gtk.org/introspection/core/1.0}class')

    class_names = set({})

    for xclass in xclasses:
        class_names.add(ns + xclass.attrib['name'])

    sections = parse_sections('hotdoc-tmp-sections.txt')
    translator = GtkDocStringFormatter()

    section_comments = {}
    class_comments = []

    for comment in wizard.comments.values():
        if not comment.name.startswith('SECTION:'):
            continue
        structure_name = comment.name.split('SECTION:')[1]
        section = sections.get(structure_name)
        if section is None:
            print "That's weird"
            continue

        section_title = section.find('TITLE')
        if section_title is not None:
            section_title = section_title.text
            if section_title in class_names:
                new_name = ('%s::%s:' % (section_title,
                    section_title))
                class_comments.append(comment)
                comment.raw_comment = comment.raw_comment.replace(comment.name,
                        new_name)
                continue

        comment.raw_comment = ''
        comment.description = translator.translate(comment.description,
                'markdown')
        if comment.short_description:
            comment.short_description = \
            translator.translate(comment.short_description, 'markdown')
        section_comments[structure_name] = comment

    return section_comments, class_comments

def patch_comments(wizard, patcher, comments):
    if not comments:
        return

    for comment in comments:
        patcher.patch(comment.filename, comment.lineno - 1,
                comment.endlineno, comment.raw_comment)
        if comment.raw_comment == '':
            for other_comment in comments:
                if (other_comment.filename == comment.filename and
                        other_comment.lineno > comment.endlineno):
                    removed = comment.endlineno - comment.lineno
                    other_comment.lineno -= removed
                    other_comment.endlineno -= removed

    if wizard.git_interface is None:
        return

    if wizard.git_interface.repo_path is not None:
        wizard.before_prompt()
        if wizard.ask_confirmation(PROMPT_COMMIT):

            for comment in comments:
                wizard.git_interface.add(comment.filename)

            commit_message = "Port to hotdoc: convert class comments"
            wizard.git_interface.commit('hotdoc', 'hotdoc@hotdoc.net', commit_message)

def translate_section_file(sections_path):
    module_path = os.path.dirname(__file__)
    trans_shscript_path = os.path.join(module_path, 'transition_scripts',
            'translate_sections.sh')
    cmd = [trans_shscript_path, sections_path, 'hotdoc-tmp-sections.txt']
    subprocess.check_call(cmd)

def port_from_gtk_doc(wizard):
    # We could not get there if c extension did not exist
    CExtClass = get_all_extension_classes(sort=False)['c-extension']
    CExtClass.validate_c_extension(wizard)
    patcher = Patcher()

    wizard.wait_for_continue(PROMPT_GTK_PORT_MAIN)
    wizard.prompt_executable('pandoc')
    sections_path = wizard.prompt_key('sections_file',
            prompt=PROMPT_SECTIONS_FILE, store=False,
            validate_function=wizard.check_path_is_file)
    translate_section_file(sections_path)

    section_comments, class_comments = get_section_comments(wizard)

    wizard.before_prompt()

    if not wizard.ask_confirmation(PROMPT_SECTIONS_CONVERSION %
            (len(section_comments), len(class_comments))):
        raise Skip

    patch_comments(wizard, patcher, class_comments +
            section_comments.values())
    sgml_path = wizard.prompt_key('sgml_path',
            prompt=PROMPT_SGML_FILE, store=False,
            validate_function=wizard.check_path_is_file)
    folder = wizard.prompt_key('markdown_folder',
            prompt=PROMPT_DESTINATION, store=False,
            validate_function=wizard.validate_folder)

    convert_to_markdown(sgml_path, 'hotdoc-tmp-sections.txt', folder,
            section_comments, 'gobject-api.markdown')

    os.unlink('hotdoc-tmp-sections.txt')

    return 'gobject-api.markdown'

PROMPT_GI_INDEX=\
"""
You will now need to provide a markdown index for introspected
symbols.

You can learn more about standalone markdown files at [FIXME],
for now suffice to say that these files provide the basic skeleton
for the output documentation, and list which symbols should be
documented in which page.

The index is the root, it will usually link to various subpages.

There are three ways to provide this index:

- Converting existing gtk-doc files.
- Generating one

You can of course skip this phase for now, and come back to it later.

"""

class GIWizard(HotdocWizard):
    def do_quick_start(self):
        if not HotdocWizard.group_prompt(self):
            return False

        res = HotdocWizard.do_quick_start(self)

        self.before_prompt()
        try:
            choice = self.propose_choice(
                    ["Create index from a gtk-doc project",
                    ],
                    extra_prompt=PROMPT_GI_INDEX
                    )

            if choice == 0:
                self.config['gi_index'] = port_from_gtk_doc(self)
        except Skip:
            pass

        return res

    def get_index_path(self):
        return 'gobject-api'

    def get_index_name(self):
        return 'GObject API'

    def group_prompt(self):
        return True

class GIExtension(BaseExtension):
    EXTENSION_NAME = "gi-extension"

    def __init__(self, doc_repo, config):
        BaseExtension.__init__(self, doc_repo, config)
        self.gir_file = config.get('gir_file')
        if self.gir_file and not os.path.exists(self.gir_file):
            self.gir_file = doc_repo.resolve_config_path(self.gir_file)

        self.gi_index = config.get('gi_index')
        self.languages = [l.lower() for l in config.get('languages', [])]
        self.language = 'c'
        self.gir_parser = None

        doc_repo.doc_tree.page_parser.register_well_known_name ('gobject-api',
                self.gi_index_handler)

        self.__nsmap = {'core': 'http://www.gtk.org/introspection/core/1.0',
                      'c': 'http://www.gtk.org/introspection/c/1.0',
                      'glib': 'http://www.gtk.org/introspection/glib/1.0'}

        self.__parsed_girs = set()
        self.__gir_root = etree.parse(self.gir_file).getroot()
        self.__node_cache = {}
        # We need to collect all class nodes and build the
        # hierarchy beforehand, because git class nodes do not
        # know about their children
        self.__class_nodes = {}

        from datetime import datetime
        n = datetime.now()
        self.__cache_nodes(self.__gir_root)
        self.__gir_hierarchies = {}
        self.__gir_children_map = defaultdict(dict)
        self.__create_hierarchies()
        print "took me", datetime.now() - n

        self.__c_names = {}
        self.__python_names = {}
        self.__javascript_names = {}

        # Make sure C always gets formatted first
        if 'c' in self.languages:
            self.languages.remove ('c')
            self.languages.insert (0, 'c')

        self.__annotation_factories = \
                {"allow-none": self.__make_allow_none_annotation,
                 "transfer": self.__make_transfer_annotation,
                 "inout": self.__make_inout_annotation,
                 "out": self.__make_out_annotation,
                 "in": self.__make_in_annotation,
                 "array": self.__make_array_annotation,
                 "element-type": self.__make_element_type_annotation,
                 "scope": self.__make_scope_annotation,
                 "closure": self.__make_closure_annotation,
                 "nullable": self.__make_nullable_annotation,
                 "type": self.__make_type_annotation,
                 "optional": self.__make_optional_annotation,
                 "default": self.__make_default_annotation,
                }

        self.formatters["html"] = GIHtmlFormatter(self,
                self.doc_repo.link_resolver)

        self.__translated_names = {}

    def __find_gir_file(self, gir_name):
        xdg_dirs = os.getenv('XDG_DATA_DIRS') or ''
        xdg_dirs = [p for p in xdg_dirs.split(':') if p]
        xdg_dirs.append(self.doc_repo.datadir)
        for dir_ in xdg_dirs:
            gir_file = os.path.join(dir_, 'gir-1.0', gir_name)
            if os.path.exists(gir_file):
                return gir_file
        return None

    def __cache_nodes(self, gir_root):
        id_key = '{%s}identifier' % self.__nsmap['c']
        for node in gir_root.xpath(
                './/*[@c:identifier]',
                namespaces=self.__nsmap):
            self.__node_cache[node.attrib[id_key]] = node

        id_type = '{%s}type' % self.__nsmap['c']
        class_tag = '{%s}class' % self.__nsmap['core']
        for node in gir_root.xpath(
                './/*[not(self::core:type) and not (self::core:array)][@c:type]',
                namespaces=self.__nsmap):
            name = node.attrib[id_type]
            self.__node_cache[name] = node
            if node.tag == class_tag:
                gi_name = '.'.join(self.__get_gi_name_components(node))
                self.__class_nodes[gi_name] = node
                self.__node_cache['%s::%s' % (name, name)] = node

        for node in gir_root.xpath(
                './/core:property',
                namespaces=self.__nsmap):
            name = '%s:%s' % (node.getparent().attrib['{%s}type' %
                self.__nsmap['c']], node.attrib['name'])
            self.__node_cache[name] = node

        for node in gir_root.xpath(
                './/glib:signal',
                namespaces=self.__nsmap):
            name = '%s::%s' % (node.getparent().attrib['{%s}type' %
                self.__nsmap['c']], node.attrib['name'])
            self.__node_cache[name] = node

        for node in gir_root.xpath(
                './/core:virtual-method',
                namespaces=self.__nsmap):
            name = '%s:::%s' % (node.getparent().attrib['{%s}type' %
                self.__nsmap['c']], node.attrib['name'])
            self.__node_cache[name] = node

        for inc in gir_root.findall('./core:include',
                namespaces = self.__nsmap):
            inc_name = inc.attrib["name"]
            inc_version = inc.attrib["version"]
            gir_file = self.__find_gir_file('%s-%s.gir' % (inc_name,
                inc_version))
            if not gir_file:
                print "Couldn't find a gir for", inc_name, inc_version
                continue

            if gir_file in self.__parsed_girs:
                continue

            self.__parsed_girs.add(gir_file)
            inc_gir_root = etree.parse(gir_file).getroot()
            self.__cache_nodes(inc_gir_root)

    def __create_hierarchies(self):
        for gi_name, klass in self.__class_nodes.iteritems():
            hierarchy = self.__create_hierarchy (klass)
            self.__gir_hierarchies[gi_name] = hierarchy

    def __get_klass_name(self, klass):
        klass_name = klass.attrib.get('{%s}type' % self.__nsmap['c'])
        if not klass_name:
            klass_name = klass.attrib.get('{%s}type-name' % self.__nsmap['glib'])
        return klass_name

    def __create_hierarchy (self, klass):
        klaass = klass
        hierarchy = []
        while (True):
            parent_name = klass.attrib.get('parent')
            if not parent_name:
                break

            if not '.' in parent_name:
                namespace = klass.getparent().attrib['name']
                parent_name = '%s.%s' % (namespace, parent_name)
            parent_class = self.__class_nodes[parent_name]
            children = self.__gir_children_map[parent_name]
            klass_name = self.__get_klass_name (klass)

            if not klass_name in children:
                link = Link(None, klass_name, klass_name)
                sym = QualifiedSymbol(type_tokens=[link])
                children[klass_name] = sym

            klass_name = self.__get_klass_name(parent_class)
            link = Link(None, klass_name, klass_name)
            sym = QualifiedSymbol(type_tokens=[link])
            hierarchy.append (sym)

            klass = parent_class

        hierarchy.reverse()
        return hierarchy

    @staticmethod
    def add_arguments (parser):
        group = parser.add_argument_group('GObject-introspection extension',
                DESCRIPTION, wizard_class=GIWizard)
        group.add_argument ("--gir-file", action="store",
                dest="gir_file",
                help="Path to the gir file of the documented library",
                finalize_function=HotdocWizard.finalize_path)
        group.add_argument ("--languages", action="store",
                nargs='*',
                help="Languages to translate documentation in (c, python, javascript)")
        group.add_argument ("--gi-index", action="store",
                dest="gi_index",
                help=("Name of the gi root markdown file, you can answer None "
                    "and follow the prompts later on to have "
                    "one created for you"),
                finalize_function=HotdocWizard.finalize_path)

    def __gather_gtk_doc_links (self):
        sgml_dir = os.path.join(self.doc_repo.datadir, "gtk-doc", "html")
        if not os.path.exists(sgml_dir):
            print "no gtk doc to gather links from in %s" % sgml_dir
            return

        for node in os.listdir(sgml_dir):
            dir_ = os.path.join(sgml_dir, node)
            if os.path.isdir(dir_):
                try:
                    self.__parse_sgml_index(dir_)
                except IOError:
                    pass

    def __parse_sgml_index(self, dir_):
        symbol_map = dict({})
        remote_prefix = ""
        with open(os.path.join(dir_, "index.sgml"), 'r') as f:
            for l in f:
                if l.startswith("<ONLINE"):
                    remote_prefix = l.split('"')[1]
                elif not remote_prefix:
                    break
                elif l.startswith("<ANCHOR"):
                    split_line = l.split('"')
                    filename = split_line[3].split('/', 1)[-1]
                    title = split_line[1].replace('-', '_')

                    if title.endswith (":CAPS"):
                        title = title [:-5]
                    if remote_prefix:
                        href = '%s/%s' % (remote_prefix, filename)
                    else:
                        href = filename

                    link = Link (href, title, title)

                    self.doc_repo.link_resolver.upsert_link (link, external=True)

    def __make_type_annotation (self, annotation, value):
        if not value:
            return None

        return Annotation("type", TYPE_HELP, value[0])

    def __make_nullable_annotation (self, annotation, value):
        return Annotation("nullable", NULLABLE_HELP)

    def __make_optional_annotation (self, annotation, value):
        return Annotation ("optional", OPTIONAL_HELP)

    def __make_allow_none_annotation(self, annotation, value):
        return Annotation ("allow-none", ALLOW_NONE_HELP)

    def __make_transfer_annotation(self, annotation, value):
        if value[0] == "none":
            return Annotation ("transfer: none", TRANSFER_NONE_HELP)
        elif value[0] == "full":
            return Annotation ("transfer: full", TRANSFER_FULL_HELP)
        elif value[0] == "floating":
            return Annotation ("transfer: floating", TRANSFER_FLOATING_HELP)
        elif value[0] == "container":
            return Annotation ("transfer: container", TRANSFER_CONTAINER_HELP)
        else:
            return None

    def __make_inout_annotation (self, annotation, value):
        return Annotation ("inout", DIRECTION_INOUT_HELP)

    def __make_out_annotation (self, annotation, value):
        return Annotation ("out", DIRECTION_OUT_HELP)

    def __make_in_annotation (self, annotation, value):
        return Annotation ("in", DIRECTION_IN_HELP)

    def __make_element_type_annotation (self, annotation, value):
        annotation_val = None
        if type(value) == list:
            annotation_val = value[0]
        return Annotation ("element-type", ELEMENT_TYPE_HELP, annotation_val)

    def __make_array_annotation (self, annotation, value):
        annotation_val = None
        if type(value) == dict:
            annotation_val = ""
            for name, val in value.iteritems():
                annotation_val += "%s=%s" % (name, val)
        return Annotation ("array", ARRAY_HELP, annotation_val)

    def __make_scope_annotation (self, annotation, value):
        if type (value) != list or not value:
            return None

        if value[0] == "async":
            return Annotation ("scope async", SCOPE_ASYNC_HELP)
        elif value[0] == "call":
            return Annotation ("scope call", SCOPE_CALL_HELP)
        elif value[0] == 'notified':
            return Annotation ("scope notified", SCOPE_NOTIFIED_HELP)
        return None

    def __make_closure_annotation (self, annotation, value):
        if type (value) != list or not value:
            return Annotation ("closure", CLOSURE_DATA_HELP)

        return Annotation ("closure", CLOSURE_HELP % value[0])

    def __make_default_annotation (self, annotation, value):
        return Annotation ("default %s" % str (value[0]), DEFAULT_HELP)

    def __create_annotation (self, annotation_name, annotation_value):
        factory = self.__annotation_factories.get(annotation_name)
        if not factory:
            return None
        return factory (annotation_name, annotation_value)

    def __make_annotations (self, parameter):
        if not parameter.comment:
            return []

        if not parameter.comment.annotations:
            return []

        annotations = []

        for ann, val in parameter.comment.annotations.iteritems():
            if ann == "skip":
                continue
            annotation = self.__create_annotation (ann, val.argument)
            if not annotation:
                print "This parameter annotation is unknown :[" + ann + "]", val.argument
                continue
            annotations.append (annotation)

        return annotations

    def __add_annotations (self, formatter, symbol):
        if self.language == 'c':
            annotations = self.__make_annotations (symbol)

            # FIXME: OK this is format time but still seems strange
            extra_content = formatter._format_annotations (annotations)
            symbol.extension_contents['Annotations'] = extra_content
        else:
            symbol.extension_contents.pop('Annotations', None)

    def __is_introspectable(self, name):
        if name in self.get_formatter('html').fundamentals:
            return True

        node = self.__node_cache.get(name)

        if node is None:
            return False

        if not name in self.__c_names:
            self.__add_translations(name, node)

        if node.attrib.get('introspectable') == '0':
            return False
        return True

    def __formatting_symbol(self, formatter, symbol):
        if type(symbol) in [ReturnItemSymbol, ParameterSymbol]:
            self.__add_annotations (formatter, symbol)

        if isinstance (symbol, QualifiedSymbol):
            return True

        # We discard symbols at formatting time because they might be exposed
        # in other languages
        if self.language != 'c':
            return self.__is_introspectable(symbol.unique_name)

        return True

    def __translate_link_ref(self, link):
        if link.ref is None:
            return None

        if self.language != 'c' and not self.__is_introspectable(link.id_):
            return '../c/' + link.ref

        return None

    def __translate_link_title(self, link):
        if self.language != 'c' and not self.__is_introspectable(link.id_):
            return link._title + ' (not introspectable)'

        return self.__translated_names.get(link.id_)

    def setup_language (self, language):
        self.language = language

        try:
            Link.resolving_link_signal.disconnect(self.__translate_link_ref)
        except KeyError:
            pass

        try:
            Link.resolving_title_signal.disconnect(self.__translate_link_title)
        except KeyError:
            pass

        try:
            self.doc_repo.doc_tree.page_parser.renaming_page_link_signal.disconnect(
                    self.__rename_page_link)
        except KeyError:
            pass

        if language is not None:
            Link.resolving_link_signal.connect(self.__translate_link_ref)
            Link.resolving_title_signal.connect(self.__translate_link_title)
            self.doc_repo.doc_tree.page_parser.renaming_page_link_signal.connect(
                    self.__rename_page_link)

        if language == 'c':
            self.__translated_names = self.__c_names
        elif language == 'python':
            self.__translated_names = self.__python_names
        elif language == 'javascript':
            self.__translated_names = self.__javascript_names
        else:
            self.__translated_names = {}

    def __unnest_type (self, parameter):
        array_nesting = 0
        array = parameter.find('{http://www.gtk.org/introspection/core/1.0}array')
        while array is not None:
            array_nesting += 1
            parameter = array
            array = parameter.find('{http://www.gtk.org/introspection/core/1.0}array')

        return parameter, array_nesting

    def __type_tokens_from_cdecl (self, cdecl):
        indirection = cdecl.count ('*')
        qualified_type = cdecl.strip ('*')
        tokens = []
        for token in qualified_type.split ():
            if token in ["const", "restrict", "volatile"]:
                tokens.append(token + ' ')
            else:
                link = Link(None, token, token)
                tokens.append (link)

        for i in range(indirection):
            tokens.append ('*')

        return tokens

    def __type_tokens_and_gi_name_from_gi_node (self, gi_node):
        type_, array_nesting = self.__unnest_type (gi_node)

        varargs = type_.find('{http://www.gtk.org/introspection/core/1.0}varargs')
        if varargs is not None:
            ctype_name = '...'
            ptype_name = 'valist'
        else:
            ptype_ = type_.find('{http://www.gtk.org/introspection/core/1.0}type')
            ctype_name = ptype_.attrib.get('{http://www.gtk.org/introspection/c/1.0}type')
            ptype_name = ptype_.attrib.get('name')

        if ctype_name is not None:
            type_tokens = self.__type_tokens_from_cdecl (ctype_name)
        elif ptype_name is not None:
            type_tokens = self.gir_parser.type_tokens_from_gitype (ptype_name)
        else:
            type_tokens = []

        namespaced = '%s.%s' % (self.gir_parser.namespace, ptype_name)
        if namespaced in self.gir_parser.gir_types:
            ptype_name = namespaced
        return type_tokens, ptype_name

    def __create_parameter_symbol (self, gi_parameter, comment):
        param_name = gi_parameter.attrib['name']
        if comment:
            param_comment = comment.params.get (param_name)
        else:
            param_comment = None

        type_tokens, gi_name = self.__type_tokens_and_gi_name_from_gi_node (gi_parameter)

        res = ParameterSymbol (argname=param_name, type_tokens=type_tokens,
                comment=param_comment)
        res.add_extension_attribute ('gi-extension', 'gi_name', gi_name)

        direction = gi_parameter.attrib.get('direction')
        if direction is None:
            direction = 'in'
        res.add_extension_attribute ('gi-extension', 'direction', direction)

        return res, direction

    def __create_return_value_symbol (self, gi_retval, comment, out_parameters):
        if comment:
            return_tag = comment.tags.get ('returns', None)
            return_comment = comment_from_tag (return_tag)
        else:
            return_comment = None

        type_tokens, gi_name = self.__type_tokens_and_gi_name_from_gi_node(gi_retval)

        if gi_name == 'none':
            ret_item = None
        else:
            ret_item = ReturnItemSymbol (type_tokens=type_tokens, comment=return_comment)

        res = [ret_item]

        for out_param in out_parameters:
            ret_item = ReturnItemSymbol (type_tokens=out_param.input_tokens,
                    comment=out_param.comment, name=out_param.argname)
            res.append(ret_item)

        return res

    def __create_parameters_and_retval (self, node, comment):
        gi_parameters = node.find('{http://www.gtk.org/introspection/core/1.0}parameters')

        if gi_parameters is None:
            instance_param = None
            gi_parameters = []
        else:
            instance_param = \
            gi_parameters.find('{http://www.gtk.org/introspection/core/1.0}instance-parameter')
            gi_parameters = gi_parameters.findall('{http://www.gtk.org/introspection/core/1.0}parameter')

        parameters = []

        if instance_param is not None:
            param, direction = self.__create_parameter_symbol (instance_param,
                    comment)
            parameters.append (param)

        out_parameters = []
        for gi_parameter in gi_parameters:
            param, direction = self.__create_parameter_symbol (gi_parameter,
                    comment)
            parameters.append (param)
            if direction != 'in':
                out_parameters.append (param)

        retval = node.find('{http://www.gtk.org/introspection/core/1.0}return-value')
        retval = self.__create_return_value_symbol (retval, comment,
                out_parameters)

        return (parameters, retval)

    def __sort_parameters (self, symbol, retval, parameters):
        in_parameters = []
        out_parameters = []

        for i, param in enumerate (parameters):
            if symbol.is_method and i == 0:
                continue

            direction = param.get_extension_attribute ('gi-extension', 'direction')

            if direction == 'in' or direction == 'inout':
                in_parameters.append (param)
            if direction == 'out' or direction == 'inout':
                out_parameters.append (param)

        symbol.add_extension_attribute ('gi-extension',
                'parameters', in_parameters)

    def __create_signal_symbol (self, node, object_name):
        name = node.attrib['name']
        unique_name = '%s::%s' % (object_name, name)
        comment = self.doc_repo.doc_database.get_comment(unique_name)

        parameters, retval = self.__create_parameters_and_retval (node, comment)
        res = self.get_or_create_symbol(SignalSymbol,
                parameters=parameters, return_value=retval,
                comment=comment, display_name=name, unique_name=unique_name)

        flags = []

        when = node.attrib.get('when')
        if when == "first":
            flags.append (RunFirstFlag())
        elif when == "last":
            flags.append (RunLastFlag())
        elif when == "cleanup":
            flags.append (RunCleanupFlag())

        no_hooks = node.attrib.get('no-hooks')
        if no_hooks == '1':
            flags.append (NoHooksFlag())

        # This is incorrect, it's not yet format time
        extra_content = self.get_formatter(self.doc_repo.output_format)._format_flags (flags)
        res.extension_contents['Flags'] = extra_content

        self.__sort_parameters (res, retval, parameters)

        return res

    def __create_property_symbol (self, node, object_name):
        name = node.attrib['name']
        unique_name = '%s:%s' % (object_name, name)
        comment = self.doc_repo.doc_database.get_comment(unique_name)

        type_tokens, gi_name = self.__type_tokens_and_gi_name_from_gi_node(node)
        type_ = QualifiedSymbol (type_tokens=type_tokens)
        type_.add_extension_attribute ('gi-extension', 'gi_name', gi_name)

        flags = []
        writable = node.attrib.get('writable')
        construct = node.attrib.get('construct')
        construct_only = node.attrib.get('construct-only')

        flags.append (ReadableFlag())
        if writable == '1':
            flags.append (WritableFlag())
        if construct_only == '1':
            flags.append (ConstructOnlyFlag())
        elif construct == '1':
            flags.append (ConstructFlag())

        res = self.get_or_create_symbol(PropertySymbol,
                prop_type=type_, comment=comment,
                display_name=name, unique_name=unique_name)

        extra_content = self.get_formatter(self.doc_repo.output_format)._format_flags (flags)
        res.extension_contents['Flags'] = extra_content

        return res

    def __create_vfunc_symbol (self, node, comment, object_name):
        name = node.attrib['name']
        unique_name = '%s:::%s' % (object_name, name)

        parameters, retval = self.__create_parameters_and_retval (node, comment)
        symbol = self.get_or_create_symbol(VFunctionSymbol,
                parameters=parameters, 
                return_value=retval, comment=comment, display_name=name,
                unique_name=unique_name)

        self.__sort_parameters (symbol, retval, parameters)

        return symbol

    def __create_class_symbol (self, symbol, gi_name):
        comment_name = '%s::%s' % (symbol.unique_name, symbol.unique_name)
        class_comment = self.doc_repo.doc_database.get_comment(comment_name)
        hierarchy = self.__gir_hierarchies[gi_name]
        children = self.__gir_children_map[gi_name]

        if class_comment:
            class_symbol = self.get_or_create_symbol(ClassSymbol,
                    hierarchy=hierarchy,
                    children=children,
                    comment=class_comment,
                    display_name=symbol.display_name,
                    unique_name=comment_name)
        else:
            class_symbol = self.get_or_create_symbol(ClassSymbol,
                    hierarchy=hierarchy, children=children,
                    display_name=symbol.display_name,
                    unique_name=comment_name)

        return class_symbol

    def __get_gi_name_components(self, node):
        parent = node.getparent()
        components = [node.attrib['name']]
        while parent is not None:
            try:
                components.insert(0, parent.attrib['name'])
            except KeyError:
                break
            parent = parent.getparent()
        return components

    def __add_translations(self, unique_name, node):
        id_key = '{%s}identifier' % self.__nsmap['c']
        id_type = '{%s}type' % self.__nsmap['c']

        components = self.__get_gi_name_components(node) 
        gi_name = '.'.join(components)

        if id_key in node.attrib:
            self.__python_names[unique_name] = gi_name
            components[-1] = 'prototype.%s' % components[-1]
            self.__javascript_names[unique_name] = '.'.join(components)
            self.__c_names[unique_name] = unique_name
        elif id_type in node.attrib:
            self.__python_names[unique_name] = gi_name
            self.__javascript_names[unique_name] = gi_name
            self.__c_names[unique_name] = unique_name

        return components, gi_name

    def __update_function (self, func, node):
        func.is_method = node.tag.endswith ('method')

        self.__add_translations(func.unique_name, node)

        gi_params, retval = self.__create_parameters_and_retval (node,
                func.comment)

        func.return_value = retval

        func_parameters = func.parameters

        if 'throws' in node.attrib:
            func_parameters = func_parameters[:-1]
            func.throws = True

        for i, param in enumerate (func_parameters):
            gi_param = gi_params[i]
            gi_name = gi_param.get_extension_attribute ('gi-extension',
                    'gi_name')
            param.add_extension_attribute ('gi-extension', 'gi_name', gi_name)
            direction = gi_param.get_extension_attribute ('gi-extension',
                    'direction')
            param.add_extension_attribute('gi-extension', 'direction',
                    direction)

        self.__sort_parameters (func, func.return_value, func_parameters)

    def __update_struct (self, symbol, node):
        symbols = []

        components, gi_name = self.__add_translations(symbol.unique_name, node)
        gi_name = '.'.join(components)

        if node.tag == '{%s}class' % self.__nsmap['core']:
            symbols.append(self.__create_class_symbol (symbol, gi_name))

        klass_name = node.attrib.get('{%s}type-name' %
                'http://www.gtk.org/introspection/glib/1.0')

        for sig_node in node.findall('./glib:signal',
                                     namespaces = self.__nsmap):
            symbols.append(self.__create_signal_symbol(
                sig_node, klass_name))

        for prop_node in node.findall('./core:property',
                                     namespaces = self.__nsmap):
            symbols.append(self.__create_property_symbol(
                prop_node, klass_name))

        class_struct_name = node.attrib.get('{%s}type-struct' %
                self.__nsmap['glib'])

        parent_comment = None
        if class_struct_name:
            class_struct_name = '%s%s' % (components[0], class_struct_name)
            parent_comment = self.doc_repo.doc_database.get_comment(class_struct_name)

        vmethods = node.findall('./core:virtual-method',
                                namespaces = self.__nsmap)

        for vfunc_node in vmethods:
            comment = None
            block = None
            if parent_comment:
                comment = parent_comment.params.get (vfunc_node.attrib['name'])
                block = Comment (name=vfunc_node.attrib['name'],
                                 description=comment.description,
                                 filename=parent_comment.filename)

            symbols.append(self.__create_vfunc_symbol (vfunc_node, block,
                                                       klass_name))

        is_gtype_struct_for = node.attrib.get('{%s}is-gtype-struct-for' %
                self.__nsmap['glib'])

        if is_gtype_struct_for is not None:
            is_gtype_struct_for = '%s%s' % (components[0], is_gtype_struct_for)
            class_node = self.__node_cache.get(is_gtype_struct_for)
            if class_node is not None:
                vmethods = class_node.findall('./core:virtual-method',
                                              namespaces = self.__nsmap)
                vnames = [vmethod.attrib['name'] for vmethod in vmethods]
                members = []
                for m in symbol.members:
                    if not m.member_name in vnames:
                        members.append(m)
                symbol.members = members

        return symbols

    def __update_symbol(self, symbol):
        node = self.__node_cache.get(symbol.unique_name)
        res = []

        if node is None:
            return res

        if isinstance(symbol, FunctionSymbol):
            self.__update_function(symbol, node)

        elif type (symbol) == StructSymbol:
            res = self.__update_struct (symbol, node)

        return res

    def __resolving_symbol (self, page, symbol):
        if page.extension_name != self.EXTENSION_NAME:
            return []

        return self.__update_symbol(symbol)

    def __rename_page_link (self, page_parser, original_name):
        return self.__translated_names.get(original_name)

    @staticmethod
    def get_dependencies ():
        return [ExtDependency('c-extension', is_upstream=True)]

    def gi_index_handler (self, doc_tree):
        index_path = find_md_file(self.gi_index, self.doc_repo.include_paths)

        return index_path, 'c', 'gi-extension'

    def setup (self):
        if not self.gir_file:
            return

        self.__gather_gtk_doc_links()
        self.gir_parser = GIRParser (self.doc_repo, self.gir_file)
        formatter = self.get_formatter(self.doc_repo.output_format)
        formatter.create_c_fundamentals()
        Page.resolving_symbol_signal.connect (self.__resolving_symbol)
        Formatter.formatting_symbol_signal.connect(self.__formatting_symbol)

    def format_page(self, page, link_resolver, base_output):
        formatter = self.get_formatter('html')
        for l in self.languages:
            formatter.set_fundamentals(l)

            self.setup_language (l)
            output = os.path.join (base_output, l)
            if not os.path.exists (output):
                os.mkdir (output)
            BaseExtension.format_page (self, page, link_resolver, output)

        self.setup_language(None)
        formatter.set_fundamentals('c')

def get_extension_classes():
    return [GIExtension]
