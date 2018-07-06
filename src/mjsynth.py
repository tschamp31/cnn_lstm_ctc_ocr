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
import numpy as np
import pipeline
# The list (well, string) of valid output characters
# If any example contains a character not found here, an error will result
# from the calls to .index in the decoder below
out_charset=pipeline.out_charset

def get_data(base_dir,file_patterns,
             num_threads=4,
             batch_size=32,
             boundaries=[32, 64, 96, 128, 160, 192, 224, 256],
             input_device=None,
             num_epoch=None,
             filter_fn=None):
    """Get input tensors bucketed by image width
    Returns:
      dataset : Dataset with elements structured as follows:
                [image, width, label, length, text, filename]
    """
    
    # Buffer size for TFRecord readers
    capacity = num_threads*batch_size*2

    # Get filenames into a dataset format
    filenames = tf.data.Dataset.from_tensor_slices(
        _get_filenames(base_dir, file_patterns))

    with tf.device(input_device): # Create bucketing batcher
        
        dataset = tf.data.TFRecordDataset(filenames, 
                                          num_parallel_reads=num_threads,
                                          buffer_size=capacity)
        dataset = dataset.prefetch(capacity)

        # Preprocess
        dataset = dataset.map(_parse_function, num_parallel_calls=num_threads)
        dataset = dataset.prefetch(capacity)

        # Filter out inappropriately dimension-ed elements
        if filter_fn:
            dataset = dataset.filter(filter_fn)

        # Batch (and bucket if necessary)
        if boundaries:
            # Bucket according to image width
            dataset = dataset.apply(tf.contrib.data.bucket_by_sequence_length(
                element_length_func=_element_length_fn,
                bucket_batch_sizes=np.full(len(boundaries) + 1, batch_size),
                bucket_boundaries=boundaries))
        else:
            # Dynamically pad batches to match largest in batch
            dataset = dataset.padded_batch(batch_size, 
                                           padded_shapes=dataset.output_shapes)

        # Repeat for num_epochs
        if num_epoch:
            dataset = dataset.repeat(num_epoch)

        # Deserialize sparse tensor
        dataset = dataset.map(
            lambda image, width, label, length, text, filename: 
            (image, 
             width, 
             tf.cast(tf.deserialize_many_sparse(label, tf.int64), 
                     tf.int32),
             length, 
             text, 
             filename),
            num_parallel_calls=num_threads)

    return dataset.prefetch(2*num_threads) # prefetch 2*num_threads*batch_size
                
def _element_length_fn(image, width, label, length, text, filename):
    return width

def _get_filenames(base_dir, file_patterns=['*.tfrecord']):
    """Get a list of record files"""
    
    # List of lists ...
    data_files = [tf.gfile.Glob(os.path.join(base_dir,file_pattern))
                  for file_pattern in file_patterns]
    # flatten
    data_files = [data_file for sublist in data_files for data_file in sublist]

    return data_files

# https://www.tensorflow.org/programmers_guide/datasets#consuming_tfrecord_data
def _parse_function(data):
    """Parse the elements of the dataset"""

    feature_map = {
        'image/encoded'  :   tf.FixedLenFeature([], dtype=tf.string, 
                                                default_value='' ),
        'image/labels'   :   tf.VarLenFeature( dtype=tf.int64 ), 
        'image/width'    :   tf.FixedLenFeature([1], dtype=tf.int64,
                                                default_value=1 ),
        'image/filename' :   tf.FixedLenFeature([], dtype=tf.string,
                                                default_value='' ),
        'text/string'    :   tf.FixedLenFeature([], dtype=tf.string,
                                                default_value='' ),
        'text/length'    :   tf.FixedLenFeature([1], dtype=tf.int64,
                                                default_value=1 )
    }
    
    features = tf.parse_single_example(data, feature_map)
    
    # Initialize fields according to feature map
    image = tf.image.decode_jpeg( features['image/encoded'], channels=1 ) #gray
    width = tf.cast( features['image/width'], tf.int32 ) # for ctc_loss
    label = tf.serialize_sparse( features['image/labels'] ) # for batching
    length = features['text/length']
    text = features['text/string']
    filename = features['image/filename']

    # Prepare image
    image = _preprocess_image(image)

    return image,width,label,length,text,filename

def _preprocess_image(image):
    # Rescale from uint8([0,255]) to float([-0.5,0.5])
    image = tf.image.convert_image_dtype(image, tf.float32)
    image = tf.subtract(image, 0.5)

    # Pad with copy of first row to expand to 32 pixels height
    first_row = tf.slice(image, [0, 0, 0], [1, -1, -1])
    image = tf.concat([first_row, image], 0)

    return image