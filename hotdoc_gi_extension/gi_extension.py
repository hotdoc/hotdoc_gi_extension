import os

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

from .gi_html_formatter import GIHtmlFormatter
from .gi_annotation_parser import GIAnnotationParser
from .gi_wizard import GIWizard


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

DESCRIPTION=\
"""
Parse a gir file and add signals, properties, classes
and virtual methods.

Can output documentation for various
languages.

Must be used in combination with the C extension.
"""


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

        self.__annotation_parser = GIAnnotationParser()

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

    def __add_annotations (self, formatter, symbol):
        if self.language == 'c':
            annotations = self.__annotation_parser.make_annotations(symbol)

            # FIXME: OK this is format time but still seems strange
            extra_content = formatter.format_annotations (annotations)
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

    def __get_gir_type (self, cur_ns, name):
        namespaced = '%s.%s' % (cur_ns, name)
        klass = self.__class_nodes.get (namespaced)
        if klass is not None:
            return klass
        return self.__class_nodes.get (name)

    def __get_namespace(self, node):
        parent = node.getparent()
        nstag = '{%s}namespace' % self.__nsmap['core']
        while parent is not None and parent.tag != nstag:
            parent = parent.getparent()

        return parent.attrib['name']

    def __type_tokens_from_gitype (self, cur_ns, ptype_name):
        qs = None

        if ptype_name == 'none':
            return None

        gitype = self.__get_gir_type (cur_ns, ptype_name)
        if gitype is not None:
            c_type = gitype.attrib['{http://www.gtk.org/introspection/c/1.0}type']
            ptype_name = c_type

        type_link = Link (None, ptype_name, ptype_name)

        tokens = [type_link]
        tokens += '*'

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

        cur_ns = self.__get_namespace(gi_node)

        if ctype_name is not None:
            type_tokens = self.__type_tokens_from_cdecl (ctype_name)
        elif ptype_name is not None:
            type_tokens = self.__type_tokens_from_gitype (cur_ns, ptype_name)
        else:
            type_tokens = []

        namespaced = '%s.%s' % (cur_ns, ptype_name)
        if namespaced in self.__class_nodes:
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
