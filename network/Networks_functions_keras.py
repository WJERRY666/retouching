
from datetime import datetime
from os import makedirs, cpu_count
from os.path import join

import numpy as np

import tensorflow as tf
from tensorflow.python.eager import context
from tensorflow.python.keras import backend as K
from tensorflow.python.keras import layers
from tensorflow.python.keras.callbacks import ModelCheckpoint, TensorBoard, LearningRateScheduler



##################### Network parameters
SCALE = 1.0
REG = 0.001


##################### Simple helper functions
def write_args(args, filepath):
	args_dict = vars(args)
	with open(filepath, 'w') as f:
		for key, value in args_dict.items():
			f.write("{:20s}: {}\n".format(key, value))


def write_history(history, filepath):
	with open(filepath, 'w') as f:
		for key, values in history.history.items():
			f.write("{}\n".format(key))
			for value in values:
				f.write("{:0.5f}\n".format(value))
			f.write("\n")


def write_result(keys, values, filepath):
	with open(filepath, 'w') as f:
		for key, value in zip(keys, values):
			f.write("{:20s}: {:0.5f}\n".format(key, value))



##################### Dataset parsing functions
# Convert bytes data into tensorflow array
def _bytes_to_array(features, key, element_type, dimension):
	return 	tf.cast(\
				tf.reshape(\
					tf.decode_raw(\
						features[key],\
						element_type),\
					dimension) ,\
				tf.float32)


# Parse dataset 
def _parse_function(example_proto):
	feature_description = {
		# mendatory informations
		'frames': tf.FixedLenFeature([], dtype=tf.string, default_value=""),
		'label'	: tf.FixedLenFeature([], dtype=tf.string, default_value=""),

		# additional information
		# 'br'	: tf.FixedLenFeature([], dtype=tf.int64, default_value=1)
	}

	# parse feature
	features = tf.parse_single_example(example_proto, feature_description)

	frames = _bytes_to_array(features, 'frames', tf.uint8, [256, 256, 3])
	frames = tf.image.rgb_to_grayscale(frames)
	label = _bytes_to_array(features, 'label', tf.uint8, [2])

	# return frames, label, br
	return frames, label


# Setup the dataset options
def configure_dataset(fnames, batch_size):
	buffer_size = max(len(fnames) / batch_size, 16) # recommend buffer_size = # of elements / batches
	buffer_size = tf.cast(buffer_size, tf.int64)

	dataset = tf.data.TFRecordDataset(fnames)
	dataset = dataset.map(_parse_function, num_parallel_calls=cpu_count())
	dataset = dataset.prefetch(buffer_size=buffer_size) 
	dataset = dataset.shuffle(buffer_size=buffer_size, reshuffle_each_iteration=True) 
	dataset = dataset.repeat()
	dataset = dataset.batch(batch_size)

	return dataset



##################### Log file manager
# custom tensorboard callback
class TrainValTensorBoard(TensorBoard):
	def __init__(self, log_dir='./logs', **kwargs):
		self.val_log_dir = join(log_dir, 'validation')
		training_log_dir = join(log_dir, 'training')
		super(TrainValTensorBoard, self).__init__(training_log_dir, **kwargs)

	def set_model(self, model):
		if context.executing_eagerly():
			self.val_writer = tf.contrib.summary.create_file_writer(self.val_log_dir)
		else:
			self.val_writer = tf.summary.FileWriter(self.val_log_dir)
		super(TrainValTensorBoard, self).set_model(model)

	def _write_custom_summaries(self, step, logs=None):
		logs = logs or {}
		val_logs = {k.replace('val_', ''): v for k, v in logs.items() if 'val_' in k}
		if context.executing_eagerly():
			with self.val_writer.as_default(), tf.contrib.summary.always_record_summaries():
				for name, value in val_logs.items():
					tf.contrib.summary.scalar(name, value.item(), step=step)
		else:
			for name, value in val_logs.items():
				summary = tf.Summary()
				summary_value = summary.value.add()
				summary_value.simple_value = value.item()
				summary_value.tag = name
				self.val_writer.add_summary(summary, step)
		self.val_writer.flush()

		logs = {k: v for k, v in logs.items() if not 'val_' in k}
		super(TrainValTensorBoard, self)._write_custom_summaries(step, logs)

	def on_train_end(self, logs=None):
		super(TrainValTensorBoard, self).on_train_end(logs)
		self.val_writer.close()


# custom learning rate scheduler callback
class CustomLearningRateScheduler(LearningRateScheduler):
	def __init__(self, schedule, verbose, LR_UPDATE_INTERVAL, LR_UPDATE_RATE):
		self.LR_UPDATE_INTERVAL = LR_UPDATE_INTERVAL
		self.LR_UPDATE_RATE = LR_UPDATE_RATE
		self.iteration = 0
		super(CustomLearningRateScheduler, self).__init__(schedule, verbose)


	def on_batch_begin(self, batch, logs=None):
		if not hasattr(self.model.optimizer, 'lr'):
			raise ValueError('Optimizer must have a "lr" attribute.')

		self.iteration += 1
		lr = float(K.get_value(self.model.optimizer.lr))
		lr = self.schedule(self.iteration, lr, self.LR_UPDATE_INTERVAL, self.LR_UPDATE_RATE)
		
		if not isinstance(lr, (float, np.float32, np.float64)):
			raise ValueError('The output of the "schedule" function should be float.')

		K.set_value(self.model.optimizer.lr, lr)


	def on_batch_end(self, batch, logs=None):
		logs = logs or {}
		logs['lr'] = K.get_value(self.model.optimizer.lr)


	def on_epoch_begin(self, epoch, logs=None):
		lr = float(K.get_value(self.model.optimizer.lr))
		if self.verbose > 0:
			print('\nIter %05d: LearningRateScheduler reducing learning rate to %10f.' % (self.iteration + 1, lr))



	def on_epoch_end(self, epoch, logs=None):
		pass

def lr_scheduler(iteration, lr, LR_UPDATE_INTERVAL, LR_UPDATE_RATE):
	if iteration % LR_UPDATE_INTERVAL == 0:
		lr *= LR_UPDATE_RATE 
	return lr


# Return callback classes
def load_callbacks(args):
	LOG_PATH 			= args.log_path
	METHOD 				= args.method
	BATCH_SIZE 			= args.batch_size
	LR_UPDATE_INTERVAL 	= args.lr_update_interval
	LR_UPDATE_RATE 		= args.lr_update_rate

	# Set log file path
	current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
	LOG_PATH = join(LOG_PATH, current_time + "_{}".format("all" if METHOD=="*" else METHOD))


	# 1. checkpoint callback
	ckpt_path = join(LOG_PATH, "checkpoint")
	makedirs(ckpt_path)
	ckpt_file = join(ckpt_path, "cp-{epoch:04d}.ckpt")
	ckpt_callback = ModelCheckpoint(filepath=ckpt_file, save_weights_only=True, verbose=1, period=1)
	

	# 2. tensorboard callback
	tb_callback = TrainValTensorBoard(log_dir=LOG_PATH, batch_size=BATCH_SIZE, update_freq='batch')


	# 3. learning rate scheduler callback
	lr_callback = CustomLearningRateScheduler(	schedule=lr_scheduler, \
												verbose=1, \
												LR_UPDATE_INTERVAL=LR_UPDATE_INTERVAL, \
										 		LR_UPDATE_RATE=LR_UPDATE_RATE)


	

	return LOG_PATH, [ckpt_callback, tb_callback, lr_callback]



##################### Network layer functions
# weights
def conv2D(filters, kernel_size, strides=(1,1)):
	filters 			= int(SCALE * filters)
	padding 			= 'same'
	data_format 		= 'channels_last'
	activation 			= None
	use_bias 			= True
	kernel_initializer 	= tf.keras.initializers.he_normal()
	bias_initializer 	= tf.keras.initializers.constant(value=0.2)
	kernel_regularizer 	= tf.keras.regularizers.l2(l=REG)
	bias_regularizer 	= None

	return layers.Conv2D(filters=filters, \
						kernel_size=kernel_size, \
						strides=strides, \
						padding=padding, \
						data_format=data_format, \
						activation=activation, \
						use_bias=use_bias, \
						kernel_initializer=kernel_initializer, \
						bias_initializer=bias_initializer, \
						kernel_regularizer=kernel_regularizer, \
						bias_regularizer=bias_regularizer)

def dense(units, use_bias=True, activation=None):
	activation 			= activation
	use_bias			= use_bias
	kernel_initializer 	= tf.keras.initializers.random_normal(mean=0, stddev=0.01)
	bias_initializer 	= 'zeros'
	kernel_regularizer 	= None
	bias_regularizer 	= None

	return layers.Dense(units=units, \
						activation=activation, \
						use_bias=use_bias, \
						kernel_initializer=kernel_initializer, \
						bias_initializer=bias_initializer, \
						kernel_regularizer=kernel_regularizer, \
						bias_regularizer=bias_regularizer)


# activations
def ReLU():
	max_value 		= None
	negative_slope 	= 0
	threshold 		= 0

	return layers.ReLU(	max_value=max_value, \
						negative_slope=negative_slope, \
						threshold=threshold)

def softmax():

	return layers.Softmax()


# pooling
def maxPooling2D(pool_size):
	strides 	= None
	padding 	= 'valid'

	return layers.MaxPool2D(pool_size=pool_size, \
							strides=strides, \
							padding=padding)

def averagePooling2D(pool_size, strides):
	padding 	= 'same'
	data_format = None

	return layers.AveragePooling2D(	pool_size=pool_size, \
									strides=strides, \
									padding=padding, \
									data_format=data_format)

def globalAveragePooling2D():
	data_format = None
	return layers.GlobalAveragePooling2D(data_format=data_format)


# manipulation
def batchNorm():
	axis 						= -1
	momentum 					= 0.9
	epsilon 					= 0.001
	center 						= True
	scale 						= True
	beta_initializer 			= 'zeros'
	gamma_initializer 			= 'ones'
	moving_mean_initializer 	= 'zeros'
	moving_variance_initializer = 'ones'
	beta_regularizer 			= None
	gamma_regularizer 			= None
	beta_constraint 			= None
	gamma_constraint 			= None
	trainable 					= True
	virtual_batch_size 			= None

	return layers.BatchNormalization(axis=-axis, \
									momentum=momentum, \
									epsilon=epsilon, \
									center=center, \
									scale=scale, \
									beta_initializer=beta_initializer, \
									gamma_initializer=gamma_initializer, \
									moving_mean_initializer=moving_mean_initializer, \
									moving_variance_initializer=moving_variance_initializer, \
									beta_regularizer=beta_regularizer, \
									gamma_regularizer=gamma_regularizer, \
									beta_constraint=beta_constraint, \
									gamma_constraint=gamma_constraint, \
									trainable=trainable, \
									virtual_batch_size=virtual_batch_size)

def flatten():

	return layers.flatten()

def add(*args):
	return layers.Add()(*args)






