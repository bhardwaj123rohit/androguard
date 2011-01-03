#!/usr/bin/env python

import sys

PATH_INSTALL = "./"
sys.path.append(PATH_INSTALL + "/core")
sys.path.append(PATH_INSTALL + "/core/bytecodes")
sys.path.append(PATH_INSTALL + "/core/analysis")

import jvm, analysis

TEST = "./examples/java/test/orig/Test1.class" 

j = jvm.JVMFormat( open(TEST).read() )
x = analysis.VM_BCA( j )

# SHOW METHODS
for i in j.get_methods() :
   i.pretty_show( x.hmethods[ i ] )