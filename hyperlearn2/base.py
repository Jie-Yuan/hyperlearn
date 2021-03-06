
from functools import wraps
from psutil import virtual_memory
import numpy as np
from scipy.linalg import lapack as _lapack, blas as _blas
from . import numba as _numba
from .numba import _min
from inspect import signature
import sys

MAX_MEMORY = 0.94
_is64 = sys.maxsize > (1 << 32)
maxFloat = np.float64 if _is64 else np.float32

memory_usage = {
	"full" : 	lambda n,p: n*p + _min(n**2, p**2),
	"same" : 	lambda n,p: n*p,
	"triu" : 	lambda n,p: p**2 if p < n else n*p,
	"squared" :	lambda n,p: n**2,
	"columns" :	lambda n,p: p,
	}
f_same_memory = memory_usage["same"]

###
def memory(shape, dtype, memcheck):
	"""
	[Edited 18/11/2018 Slightly faster]
	Checks if an operation on a matrix is within memory bounds.

	input:		3 arguments
	----------------------------------------------------------
	X:			Input matrix
	dtype:		Input datatype(matrix)
	memcheck:	lambda n,p: ... function or f(n,p)

	returns: 	2 arguments
	----------------------------------------------------------
	need:		Total memory required for operation
	surplus:	Boolean - True means within memory bounds.
	"""
	if memcheck == None: return 0
	byte = np.dtype(dtype).itemsize

	if len(shape) == 1:
		shape = (1, shape[0])

	multiplier = memcheck(*shape)
	need = (multiplier * byte) >> 20 # 10 == KB, 20 == MB
	return need

###
def arg_process(x, square):
	# Internal checks if object is a matrix and checks the datatype
	if type(x) == np.ndarray:
		shape = x.shape
		if len(shape) > 1:
			if square:
				# matrix must be a square one (n == p)
				if shape[0] != shape[1]:
					raise AssertionError(f"2D array is not square. Dimensions seen are {shape}.")
			# if float:
			dtype, dt = x.dtype, x.dtype
			if dtype == np.float32 or dtype == maxFloat or np.issubdtype(dtype, np.complexfloating):
				return 0, False
			elif np.issubdtype(dtype, np.integer):
				# conversion is needed, so check memory
				dt = maxFloat if (dtype == np.uint64 or dtype == np.int64) else np.float32
			elif dtype == np.float16:
				# float16 is not supported on CPU
				dt = np.float32
			else:
				# not any numerical data dtype
				raise TypeError(f"Data type of {dtype} is not a numerical type.")
			# calculate memory usage if dtype needs to be converted
			if dt != dtype:
				return memory(shape, dt, f_same_memory), dt
	return 0, None

###
def process(f = None, memcheck = None, square = False):
	"""
	[Added 14/11/2018] [Edited 18/11/2018 for speed]
	Decorator onto HyperLearn functions. Does 2 things:
	1. Convert datatypes to appropriate ones
	2. Convert matrices to arrays
	3. (Optional) checks memory bounds.

	input:		1 argument, 1 optional
	----------------------------------------------------------
	f:			The function to be decorated
	memcheck:	lambda n,p: ... function or f(n,p)

	returns: 	X arguments from function f
	----------------------------------------------------------
	"""
	# convert all memory arguments to function checks
	if memcheck != None:
		if type(memcheck) == str:
			memcheck = {"X":memcheck}
		if type(memcheck) == dict:
			for key in memcheck:
				try:
					memcheck[key] = memory_usage[memcheck[key]]
				except:
					raise KeyError(f"Memory usage argument for {key} not recognised.")
	###
	def decorate(f):
		# get function signature
		memory_length = len(memcheck)
		memory_keys = list(memcheck.keys())
		function_signature = signature(f)
		function_args = function_signature.parameters

		@wraps(f)
		def wrapper(*args, **kwargs):
			no_args = len(function_args)
			l = len(args)
			L = len(kwargs)
			size = l + L

			if size == 0 or l == 0:
				# No arguments seen
				raise IndexError("Function needs >= 1 function arguments.")

			# determine if kwargs names are within the scope of the function
			if size > no_args:
				raise IndexError(f"Function has too many inputs. Only {no_args} is needed.")
				
			# check if first item is an array
			X = args[0]
			if type(X) == np.matrix:
				if X.shape[0] == 1:
					X = X.A1 # flatten down
				else:
					X = X.A
				args[0] = X
			if type(X) != np.ndarray:
				raise IndexError("First argument is not a 2D array. Must be an array.")

			# check booleans and if an array is seen
			otherYes = 0
			duplicate = np.zeros(l, dtype = bool)

			for i,x in enumerate(args):
				if type(x) == bool:
					kwargs[memory_keys[i]] = x
					otherYes += x
					duplicate[i] = True

			# check function arguments
			for x in kwargs:
				try:
					check_arg = function_args[x]
					t = kwargs[x]
					if type(t) == bool:
						otherYes += t
				except:
					raise NameError("Argument '{x}' is not recognised in function. "
					f"Function accepted signature is {function_signature}.")
					
			# update X memory check
			if otherYes == memory_length-1 or otherYes == 0:
				for i in kwargs:
					t = kwargs[i]
					if type(t) == bool:
						kwargs[i] = False # set to all False
				kwargs["X"] = True # all true
			else:
				kwargs["X"] = False
				
			# kwargs leftovers
			for x in memory_keys:
				try:
					i = kwargs[x]
				except:
					kwargs[x] = False
				
			# Now check data types of arrays
			need = 0 # how much memory needed
			new_dtypes = [] # new datatypes

			for i in range(l):
				x = args[i]
				if i != 0:
					if type(x) == np.matrix:
						args[i] = x.A1 if x.shape[0] == 1 else x.A
						x = args[i]
				n, dtype = arg_process(x, square)
				if i == 0:
					if dtype == False:
						X_dtype = x.dtype
						dtype = None
					else:
						X_dtype = dtype
				need += n
				new_dtypes.append(dtype)
				
			for i in kwargs:
				x = kwargs[i]
				if type(x) == np.matrix:
					kwargs[i] = x.A1 if x.shape[0] == 1 else x.A
					x = kwargs[i]
				n, dtype = arg_process(x, square)
				need += n
				new_dtypes.append(dtype)
				

			# check X satisfying boolean arguments in order of importance
			shape = X.shape
			for i in memory_keys:
				if kwargs[i] == True:
					need += memory(shape, X_dtype, memcheck[i])
					break
				
			# confirm memory is enough for data conversion
			if need > 0:
				free = int(virtual_memory().available * MAX_MEMORY) >> 20
				if need > free:
					raise MemoryError(f"Operation requires {need} MB, but {free} MB is free, "
				f"so an extra {need-free} MB is required.")
					
			# convert data dtypes
			arg = 0
			for i in range(l):
				dtype = new_dtypes[arg]
				if dtype != None:
					args[i] = args[i].astype(dtype)
				arg += 1
			for i in kwargs:
				dtype = new_dtypes[arg]
				if dtype != None:
					kwargs[i] = kwargs[i].astype(dtype)
				arg += 1
				
			# clean up args so no duplicates are seen
			l -= 1
			while l > 0:
				if duplicate[l]:
					del args[l]
				l -= 1
			del kwargs["X"]  # no need for first argument

			# finally execute function
			try:
				return f(*args, **kwargs)
			except MemoryError:
				# Memory Error again --> didnt catch
				raise MemoryError(f"Operation requires more memory than what the system resources offer.")
		return wrapper

	if f:
		return decorate(f)
	return decorate
	
###
class lapack():
	"""
	[Added 14/11/2018]
	Get a LAPACK function based on the dtype(X). Acts like Scipy's get lapack function.

	input:		1 argument, 2 optional
	----------------------------------------------------------
	function:	String for lapack function eg: "getrf"
	turbo:		Boolean to indicate if float32 can be used.
	numba:		String for numba function.

	returns: 	LAPACK or Numba function.
	----------------------------------------------------------
	"""
	def __init__(self, function, numba = None, turbo = True):
		self.function = function
		self.turbo = turbo
		self.f = None

		if numba != None:
			try: 
				self.f = eval(f'_numba.{numba}')
				self.function = numba
			except: pass

	def __call__(self, *args, **kwargs):
		if self.f == None:

			if len(args) > 0:
				dtype = args[0].dtype
			else:
				dtype = next(iter(kwargs.values())).dtype
			
			if dtype == np.float32 and self.turbo:
				self.f = f"_lapack.s{self.function}"
			elif dtype == np.float64 or not self.turbo:
				self.f = f"_lapack.d{self.function}"
			elif dtype == np.complex64:
				self.f = f"_lapack.c{self.function}"
			else:
				self.f = f"_lapack.z{self.function}"
			self.f = eval(self.f)

		return self.f(*args, **kwargs)

###
class blas():
	"""
	[Added 14/11/2018]
	Get a BLAS function based on the dtype(X). Acts like Scipy's get blas function.

	input:		1 argument
	----------------------------------------------------------
	function:	String for blas function eg: "getrf"

	returns: 	BLAS function
	----------------------------------------------------------
	"""
	def __init__(self, function):
		self.function = function
		self.f = None

	def __call__(self, *args, **kwargs):
		if self.f == None:
			if len(args) > 0:
				dtype = args[0].dtype
			else:
				dtype = next(iter(kwargs.values())).dtype
			
			if dtype == np.float32:
				self.f = f"_blas.s{self.function}"
			elif dtype == np.float64:
				self.f = f"_blas.d{self.function}"
			elif dtype == np.complex64:
				self.f = f"_blas.c{self.function}"
			else:
				self.f = f"_blas.z{self.function}"
			self.f = eval(self.f)

		return self.f(*args, **kwargs)
