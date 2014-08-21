# This file is part of Androguard.
#
# Copyright (c) 2012 Geoffroy Gueguen <geoffroy.gueguen@gmail.com>
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
sys.path.append('./')

import logging
from collections import defaultdict
import androguard.core.androconf as androconf
import androguard.decompiler.dad.util as util
from androguard.core.analysis import analysis
from androguard.core.bytecodes import apk, dvm
from androguard.decompiler.dad.control_flow import identify_structures
from androguard.decompiler.dad.dataflow import (build_def_use,
                                                place_declarations,
                                                dead_code_elimination,
                                                register_propagation,
                                                split_variables)
from androguard.decompiler.dad.graph import construct
from androguard.decompiler.dad.instruction import Param, ThisParam
from androguard.decompiler.dad.writer import Writer


def auto_vm(filename):
    ret = androconf.is_android(filename)
    if ret == 'APK':
        return dvm.DalvikVMFormat(apk.APK(filename).get_dex())
    elif ret == 'DEX':
        return dvm.DalvikVMFormat(open(filename, 'rb').read())
    elif ret == 'ODEX':
        return dvm.DalvikOdexVMFormat(open(filename, 'rb').read())
    return None


class DvMethod():
    def __init__(self, methanalysis):
        method = methanalysis.get_method()
        self.start_block = next(methanalysis.get_basic_blocks().get(), None)
        self.cls_name = method.get_class_name()
        self.name = method.get_name()
        self.lparams = []
        self.var_to_name = defaultdict()
        self.writer = None
        self.graph = None

        self.access = util.get_access_method(method.get_access_flags())

        desc = method.get_descriptor()
        self.type = desc.split(')')[-1]
        self.params_type = util.get_params_type(desc)

        self.exceptions = methanalysis.exceptions.exceptions

        code = method.get_code()
        if code is None:
            logger.debug('No code : %s %s', self.name, self.cls_name)
        else:
            start = code.registers_size - code.ins_size
            if 'static' not in self.access:
                self.var_to_name[start] = ThisParam(start, self.name)
                self.lparams.append(start)
                start += 1
            num_param = 0
            for ptype in self.params_type:
                param = start + num_param
                self.lparams.append(param)
                self.var_to_name[param] = Param(param, ptype)
                num_param += util.get_type_size(ptype)
        if not __debug__:
            from androguard.core import bytecode
            bytecode.method2png('/tmp/dad/graphs/%s#%s.png' % \
                (self.cls_name.split('/')[-1][:-1], self.name), methanalysis)

    def process(self):
        logger.debug('METHOD : %s', self.name)

        # Native methods... no blocks.
        if self.start_block is None:
            logger.debug('Native Method.')
            self.writer = Writer(None, self)
            self.writer.write_method()
            return

        graph = construct(self.start_block, self.var_to_name, self.exceptions)
        self.graph = graph

        if not __debug__:
            util.create_png(self.cls_name, self.name, graph, '/tmp/dad/blocks')

        use_defs, def_uses = build_def_use(graph, self.lparams)
        split_variables(graph, self.var_to_name, def_uses, use_defs)
        # TODO: split_variables should update DU/UD
        use_defs, def_uses = build_def_use(graph, self.lparams)

        dead_code_elimination(graph, def_uses, use_defs)
        register_propagation(graph, def_uses, use_defs)
        
        place_declarations(graph, self.var_to_name, def_uses, use_defs)
        del def_uses, use_defs
        # After the DCE pass, some nodes may be empty, so we can simplify the
        # graph to delete these nodes.
        # We start by restructuring the graph by spliting the conditional nodes
        # into a pre-header and a header part.
        graph.split_if_nodes()
        # We then simplify the graph by merging multiple statement nodes into
        # a single statement node when possible. This also delete empty nodes.

        graph.simplify()
        graph.compute_rpo()

        if not __debug__:
            util.create_png(self.cls_name, self.name, graph,
                                                    '/tmp/dad/pre-structured')

        identify_structures(graph, graph.immediate_dominators())

        if not __debug__:
            util.create_png(self.cls_name, self.name, graph,
                                                    '/tmp/dad/structured')

        self.writer = Writer(graph, self)
        self.writer.write_method()
        del graph

    def show_source(self):
        print self.get_source()

    def get_source(self):
        if self.writer:
            return '%s' % self.writer
        return ''

    def __repr__(self):
        return 'Method %s' % self.name


class DvClass():
    def __init__(self, dvclass, vma):
        name = dvclass.get_name()
        if name.find('/') > 0:
            pckg, name = name.rsplit('/', 1)
        else:
            pckg, name = '', name
        self.package = pckg[1:].replace('/', '.')
        self.name = name[:-1]

        self.vma = vma
        self.methods = dict((meth.get_method_idx(), meth)
                            for meth in dvclass.get_methods())
        self.fields = dict((field.get_name(), field)
                           for field in dvclass.get_fields())
        self.subclasses = {}
        self.code = []
        self.inner = False

        access = dvclass.get_access_flags()
        # If interface we remove the class and abstract keywords
        if 0x200 & access:
            prototype = '%s %s'
            access -= 0x400
        else:
            prototype = '%s class %s'

        self.access = util.get_access_class(access)
        self.prototype = prototype % (' '.join(self.access), self.name)

        self.interfaces = dvclass.interfaces
        self.superclass = dvclass.get_superclassname()

        logger.info('Class : %s', self.name)
        logger.info('Methods added :')
        for index, meth in self.methods.iteritems():
            logger.info('%s (%s, %s)', index, self.name, meth.name)
        logger.info('')

    def add_subclass(self, innername, dvclass):
        self.subclasses[innername] = dvclass
        dvclass.inner = True

    def get_methods(self):
        return self.methods

    def process_method(self, num):
        methods = self.methods
        if num in methods:
            method = methods[num]
            if not isinstance(method, DvMethod):
                method.set_instructions([i for i in method.get_instructions()])
                meth = methods[num] = DvMethod(self.vma.get_method(method))
                meth.process()
                method.set_instructions([])
            else:
                method.process()
        else:
            logger.error('Method %s not found.', num)

    def process(self):
        for klass in self.subclasses.values():
            klass.process()
        for meth in self.methods:
            self.process_method(meth)

    def get_source(self):
        source = []
        if not self.inner and self.package:
            source.append('package %s;\n' % self.package)

        if self.superclass is not None:
            self.superclass = self.superclass[1:-1].replace('/', '.')
            if self.superclass.split('.')[-1] == 'Object':
                self.superclass = None
            if self.superclass is not None:
                self.prototype += ' extends %s' % self.superclass
        if self.interfaces is not None:
            interfaces = self.interfaces[1:-1].split(' ')
            self.prototype += ' implements %s' % ', '.join(
                        [n[1:-1].replace('/', '.') for n in interfaces])

        source.append('%s {\n' % self.prototype)
        for name, field in sorted(self.fields.iteritems()):
            access = util.get_access_field(field.get_access_flags())
            f_type = util.get_type(field.get_descriptor())
            source.append('    ')
            if access:
                source.append(' '.join(access))
                source.append(' ')
            if field.init_value:
                value = field.init_value.value
                if f_type == 'String':
                    value = '"%s"' % value
                source.append('%s %s = %s;\n' % (f_type, name, value))
            else:
                source.append('%s %s;\n' % (f_type, name))

        for klass in self.subclasses.values():
            source.append(klass.get_source())

        for _, method in self.methods.iteritems():
            if isinstance(method, DvMethod):
                source.append(method.get_source())
        source.append('}\n')
        return ''.join(source)

    def show_source(self):
        print self.get_source()

    def __repr__(self):
        if not self.subclasses:
            return 'Class(%s)' % self.name
        return 'Class(%s) -- Subclasses(%s)' % (self.name, self.subclasses)


class DvMachine():
    def __init__(self, name):
        vm = auto_vm(name)
        if vm is None:
            raise ValueError('Format not recognised: %s' % name)
        self.vma = analysis.uVMAnalysis(vm)
        self.classes = dict((dvclass.get_name(), dvclass)
                            for dvclass in vm.get_classes())
        #util.merge_inner(self.classes)

    def get_classes(self):
        return self.classes.keys()

    def get_class(self, class_name):
        for name, klass in self.classes.iteritems():
            if class_name in name:
                if isinstance(klass, DvClass):
                    return klass
                dvclass = self.classes[name] = DvClass(klass, self.vma)
                return dvclass

    def process(self):
        for name, klass in self.classes.iteritems():
            logger.info('Processing class: %s', name)
            if isinstance(klass, DvClass):
                klass.process()
            else:
                dvclass = self.classes[name] = DvClass(klass, self.vma)
                dvclass.process()

    def show_source(self):
        for klass in self.classes.values():
            klass.show_source()

    def process_and_show(self):
        for name, klass in sorted(self.classes.iteritems()):
            logger.info('Processing class: %s', name)
            if not isinstance(klass, DvClass):
                klass = DvClass(klass, self.vma)
            klass.process()
            klass.show_source()


logger = logging.getLogger('dad')
sys.setrecursionlimit(5000)


def main():
    # logger.setLevel(logging.DEBUG) for debugging output
    # comment the line to disable the logging.
    logger.setLevel(logging.INFO)
    console_hdlr = logging.StreamHandler(sys.stdout)
    console_hdlr.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    logger.addHandler(console_hdlr)

    default_file = 'examples/android/TestsAndroguard/bin/TestActivity.apk'
    if len(sys.argv) > 1:
        machine = DvMachine(sys.argv[1])
    else:
        machine = DvMachine(default_file)

    logger.info('========================')
    logger.info('Classes:')
    for class_name in sorted(machine.get_classes()):
        logger.info(' %s', class_name)
    logger.info('========================')

    cls_name = raw_input('Choose a class: ')
    if cls_name == '*':
        machine.process_and_show()
    else:
        cls = machine.get_class(cls_name)
        if cls is None:
            logger.error('%s not found.', cls_name)
        else:
            logger.info('======================')
            for method_id, method in cls.get_methods().items():
                logger.info('%d: %s', method_id, method.name)
            logger.info('======================')
            meth = raw_input('Method: ')
            if meth == '*':
                logger.info('CLASS = %s', cls)
                cls.process()
            else:
                cls.process_method(int(meth))
            logger.info('Source:')
            logger.info('===========================')
            cls.show_source()

if __name__ == '__main__':
    main()

