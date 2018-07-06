# CNN-LSTM-CTC-OCR
# Copyright (C) 2017 Jerod Weinman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import tensorflow as tf
from tensorflow.contrib import learn

import pipeline
import model
import filters

FLAGS = tf.app.flags.FLAGS

tf.app.flags.DEFINE_string('output','../data/model',
                          """Directory for event logs and checkpoints""")
tf.app.flags.DEFINE_string('tune_from','',
                          """Path to pre-trained model checkpoint""")
tf.app.flags.DEFINE_string('tune_scope','',
                          """Variable scope for training""")

tf.app.flags.DEFINE_integer('batch_size',2**5,
                            """Mini-batch size""")
tf.app.flags.DEFINE_float('learning_rate',1e-4,
                          """Initial learning rate""")
tf.app.flags.DEFINE_float('momentum',0.9,
                          """Optimizer gradient first-order momentum""")
tf.app.flags.DEFINE_float('decay_rate',0.9,
                          """Learning rate decay base""")
tf.app.flags.DEFINE_float('decay_steps',2**16,
                          """Learning rate decay exponent scale""")
tf.app.flags.DEFINE_boolean('decay_staircase',False,
                          """Staircase learning rate decay by integer division""")


tf.app.flags.DEFINE_integer('max_num_steps', 2**21,
                            """Number of optimization steps to run""")

tf.app.flags.DEFINE_string('train_device','/gpu:1',
                           """Device for training graph placement""")
tf.app.flags.DEFINE_string('input_device','/gpu:0',
                           """Device for preprocess/batching graph placement""")
tf.app.flags.DEFINE_boolean('static_data', True,
                            """Whether to use static data (false for dynamic data)""")
tf.app.flags.DEFINE_string('train_path','../data/train/',
                           """Base directory for training data""")
tf.app.flags.DEFINE_string('filename_pattern','words-*',
                           """File pattern for input data""")
tf.app.flags.DEFINE_integer('num_input_threads',4,
                          """Number of readers for input data""")
tf.app.flags.DEFINE_integer('width_threshold',None,
                            """Limit of input image width""")
tf.app.flags.DEFINE_integer('length_threshold',None,
                            """Limit of input string length width""")
tf.app.flags.DEFINE_integer('minimum_width',20,
                            """Set the minimum image width""")
tf.logging.set_verbosity(tf.logging.INFO)

# Non-configurable parameters
optimizer='Adam'
mode = learn.ModeKeys.TRAIN # 'Configure' training mode for dropout layers

def get_data_iterator():
    if(FLAGS.static_data):
        ds = pipeline.get_static_data(FLAGS.train_path, 
                                      str.split(FLAGS.filename_pattern,','),
                                      num_threads=FLAGS.num_input_threads,
                                      batch_size=FLAGS.batch_size,
                                      input_device=FLAGS.input_device,
                                      filter_fn=None)
                                    
    else:
        ds = pipeline.get_dynamic_data(num_threads=FLAGS.num_input_threads,
                                       batch_size=FLAGS.batch_size,
                                       input_device=FLAGS.input_device,
                                       filter_fn=filters.dyn_filter_by_width)
    return ds.make_one_shot_iterator()

def _get_training(rnn_logits,label,sequence_length):
    """Set up training ops"""
    with tf.name_scope("train"):

        if FLAGS.tune_scope:
            scope=FLAGS.tune_scope
        else:
            scope="convnet|rnn"

        rnn_vars = tf.get_collection( tf.GraphKeys.TRAINABLE_VARIABLES,
                                       scope=scope)

        loss = model.ctc_loss_layer(rnn_logits,label,sequence_length) 

        # Update batch norm stats [http://stackoverflow.com/questions/43234667]
        extra_update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)

        with tf.control_dependencies(extra_update_ops):

            learning_rate = tf.train.exponential_decay(
                FLAGS.learning_rate,
                tf.train.get_global_step(),
                FLAGS.decay_steps,
                FLAGS.decay_rate,
                staircase=FLAGS.decay_staircase,
                name='learning_rate')

            optimizer = tf.train.AdamOptimizer(
                learning_rate=learning_rate,
                beta1=FLAGS.momentum)
            
            train_op = tf.contrib.layers.optimize_loss(
                loss=loss,
                global_step=tf.train.get_global_step(),
                learning_rate=learning_rate, 
                optimizer=optimizer,
                variables=rnn_vars)

            tf.summary.scalar( 'learning_rate', learning_rate )

    return train_op


def _get_session_config():
    """Setup session config to soften device placement"""

    config=tf.ConfigProto(
        allow_soft_placement=True, 
        log_device_placement=False)

    return config


def _get_init_pretrained():
    """Return lambda for reading pretrained initial model"""
    
    if not FLAGS.tune_from:
        return None
    
    saver_reader = tf.train.Saver(
        tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES))
    
    ckpt_path=FLAGS.tune_from

    init_fn = lambda sess: saver_reader.restore(sess, ckpt_path)

    return init_fn


def main(argv=None):

    with tf.Graph().as_default():
        input_stream = get_data_iterator()
        global_step = tf.train.get_or_create_global_step()

        # Kinda gross... dynamic data doesn't have a filename element to unpack
        if(FLAGS.static_data):
            image, width, label, _, _, _ = input_stream.get_next()
        else:
            image, width, label, _, _ = input_stream.get_next()

        with tf.device(FLAGS.train_device):
            features,sequence_length = model.convnet_layers(image, width, mode)
            logits = model.rnn_layers(features, sequence_length,
                                       pipeline.num_classes())
            train_op = _get_training(logits,label,sequence_length)

        session_config = _get_session_config()

        summary_op = tf.summary.merge_all()
        init_op = tf.group( tf.global_variables_initializer(),
                            tf.local_variables_initializer())

        init_scaffold = tf.train.Scaffold(
            init_op=init_op,
            init_fn=_get_init_pretrained()
        )

        summary_hook = tf.train.SummarySaverHook(
            output_dir=FLAGS.output,
            save_secs=30,
            summary_op=summary_op
        )
        
        saver_hook = tf.train.CheckpointSaverHook(
            checkpoint_dir=FLAGS.output,
            save_secs=150
        )

        monitor = tf.train.MonitoredTrainingSession(
            checkpoint_dir=FLAGS.output, # Necessary to restore
            hooks=[summary_hook,saver_hook],
            config=session_config,
            scaffold=init_scaffold       # Scaffold initializes session
        )
        
        with monitor as sess:
            step = sess.run(global_step)
            while step < FLAGS.max_num_steps:
                if monitor.should_stop():
                    break
                [step_loss,step]=sess.run([train_op, global_step])
                
if __name__ == '__main__':
    tf.app.run()