import argparse
import math
import h5py
import numpy as np
import tensorflow as tf
import socket
import importlib
import os
import sys
import time
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)
sys.path.append(os.path.join(BASE_DIR, 'models'))
sys.path.append(os.path.join(BASE_DIR, 'utils'))
import provider
import tf_util
tf.placeholder = tf.compat.v1.placeholder
tf.train = tf.compat.v1.train
tf.to_int64 = tf.compat.v1.to_int64
tf.ConfigProto = tf.compat.v1.ConfigProto
tf.Session = tf.compat.v1.Session
tf.summary = tf.compat.v1.summary
tf.global_variables_initializer = tf.compat.v1.global_variables_initializer
from tf_sampling import the_first_n_visu, farthest_point_sample, my_point_sample, my_point_sample_neighbor, my_point_sample_featured
from scipy.spatial import distance
from transform_nets import input_transform_net, feature_transform_net

# python train.py --num_point=128 --max_epoch=100 --sample=featured --sample_batch_num=256
parser = argparse.ArgumentParser()
parser.add_argument('--gpu', type=int, default=0, help='GPU to use [default: GPU 0]')
parser.add_argument('--model', default='pointnet_cls', help='Model name: pointnet_cls or pointnet_cls_basic [default: pointnet_cls]')
parser.add_argument('--log_dir', default='log', help='Log dir [default: log]')
parser.add_argument('--num_point', type=int, default=1024, help='Point Number [256/512/1024/2048] [default: 1024]')
parser.add_argument('--max_epoch', type=int, default=250, help='Epoch to run [default: 250]')
parser.add_argument('--batch_size', type=int, default=32, help='Batch Size during training [default: 32]')
parser.add_argument('--learning_rate', type=float, default=0.001, help='Initial learning rate [default: 0.001]')
parser.add_argument('--momentum', type=float, default=0.9, help='Initial learning rate [default: 0.9]')
parser.add_argument('--optimizer', default='adam', help='adam or momentum [default: adam]')
parser.add_argument('--decay_step', type=int, default=200000, help='Decay step for lr decay [default: 200000]')
parser.add_argument('--decay_rate', type=float, default=0.7, help='Decay rate for lr decay [default: 0.8]')
parser.add_argument('--sample', type=str, default='none', help='Sampling')
parser.add_argument('--sample_batch_num', type=int, default=256, help='Batch Num during sampling [default: 256]')
FLAGS = parser.parse_args()


BATCH_SIZE = FLAGS.batch_size
NUM_POINT = FLAGS.num_point
MAX_EPOCH = FLAGS.max_epoch
BASE_LEARNING_RATE = FLAGS.learning_rate
GPU_INDEX = FLAGS.gpu
MOMENTUM = FLAGS.momentum
OPTIMIZER = FLAGS.optimizer
DECAY_STEP = FLAGS.decay_step
DECAY_RATE = FLAGS.decay_rate
SAMPLING = FLAGS.sample
SAMPLE_BATCH_NUM = FLAGS.sample_batch_num

MODEL = importlib.import_module(FLAGS.model) # import network module
MODEL_FILE = os.path.join(BASE_DIR, 'models', FLAGS.model+'.py')
LOG_DIR = FLAGS.log_dir
if not os.path.exists(LOG_DIR): os.mkdir(LOG_DIR)
os.system('cp %s %s' % (MODEL_FILE, LOG_DIR)) # bkp of model def
os.system('cp train.py %s' % (LOG_DIR)) # bkp of train procedure
LOG_FOUT = open(os.path.join(LOG_DIR, 'log_train.txt'), 'w')
LOG_FOUT.write(str(FLAGS)+'\n')

MAX_NUM_POINT = 2048
NUM_CLASSES = 40

BN_INIT_DECAY = 0.5
BN_DECAY_DECAY_RATE = 0.5
BN_DECAY_DECAY_STEP = float(DECAY_STEP)
BN_DECAY_CLIP = 0.99

HOSTNAME = socket.gethostname()

# ModelNet40 official train/test split
TRAIN_FILES = provider.getDataFiles( \
    os.path.join(BASE_DIR, 'data/modelnet40_ply_hdf5_2048/train_files.txt'))
TEST_FILES = provider.getDataFiles(\
    os.path.join(BASE_DIR, 'data/modelnet40_ply_hdf5_2048/test_files.txt'))

# if SAMPLING == 'mine_neighbor':
#     train_file_idxs = np.arange(0, len(TRAIN_FILES))
#     current_data, current_label = provider.loadDataFile(TRAIN_FILES[train_file_idxs[0]])
#     points_distance = np.zeros([len(TRAIN_FILES), len(current_data)])
#     points_distance = points_distance.astype(object)
#     print(points_distance.shape)
#     for fn in range(len(TRAIN_FILES)):
#         current_data, current_label = provider.loadDataFile(TRAIN_FILES[train_file_idxs[fn]])
#         for item in range(len(current_data)):
#             # points_distance[fn] = np.zeros([len(current_data)])
#             dist = distance.squareform(distance.pdist(current_data[item]))
#             points_distance[fn][item] = dist

def log_string(out_str):
    LOG_FOUT.write(out_str+'\n')
    LOG_FOUT.flush()
    print(out_str)


def get_learning_rate(batch):
    learning_rate = tf.train.exponential_decay(
                        BASE_LEARNING_RATE,  # Base learning rate.
                        batch * BATCH_SIZE,  # Current index into the dataset.
                        DECAY_STEP,          # Decay step.
                        DECAY_RATE,          # Decay rate.
                        staircase=True)
    learning_rate = tf.maximum(learning_rate, 0.00001) # CLIP THE LEARNING RATE!
    return learning_rate        

def get_bn_decay(batch):
    bn_momentum = tf.train.exponential_decay(
                      BN_INIT_DECAY,
                      batch*BATCH_SIZE,
                      BN_DECAY_DECAY_STEP,
                      BN_DECAY_DECAY_RATE,
                      staircase=True)
    bn_decay = tf.minimum(BN_DECAY_CLIP, 1 - bn_momentum)
    return bn_decay

def train():
    with tf.Graph().as_default() as g:
        item_num = 1
        item_point_num = 2048
        neighbor_num = 8
        x_in = tf.placeholder("float", [item_num, item_point_num, 2 * neighbor_num + 1, 3])
        # x = tf.reshape(x_in, [item_num, 2 * neighbor_num + 1, 3, 1])
        bn_decay = None

        # print(x_in.shape)

        with tf.variable_scope('transform_net1') as sc:
            x = tf.reshape(x_in, [item_point_num, 2 * neighbor_num + 1, 3])
            transform = input_transform_net(x, tf.constant(True), bn_decay, K=3)
        point_cloud_transformed = tf.matmul(x, transform)
        input_image = tf.expand_dims(point_cloud_transformed, 0)

        conv1_w = tf.get_variable("conv1_w", [1, 2 * neighbor_num + 1, 3, 64], initializer=tf.compat.v1.keras.initializers.glorot_normal())
        net = tf.nn.conv2d(input_image, conv1_w, [1, 1, 1, 1], "VALID")
        net = tf_util.batch_norm_for_conv2d(net, tf.constant(True), bn_decay=True, scope='bn1')
        net = tf.nn.relu(net)
        # print(net.shape)

        conv2_w = tf.get_variable("conv2_w", [1, 1, 64, 64], initializer=tf.compat.v1.keras.initializers.glorot_normal())
        net = tf.nn.conv2d(net, conv2_w, [1, 1, 1, 1], "VALID")
        net = tf_util.batch_norm_for_conv2d(net, tf.constant(True), bn_decay=True, scope='bn2')
        net = tf.nn.relu(net)
        # # print(net.shape)

        with tf.variable_scope('transform_net2') as sc:
            transform = feature_transform_net(net, tf.constant(True), bn_decay=True, K=64)
        net_transformed = tf.matmul(tf.squeeze(net, axis=[2]), transform)
        net_transformed = tf.expand_dims(net_transformed, [2])

        conv3_w = tf.get_variable("conv3_w", [1, 1, 64, 64], initializer=tf.compat.v1.keras.initializers.glorot_normal())
        net = tf.nn.conv2d(net_transformed, conv3_w, [1, 1, 1, 1], "VALID")
        net = tf_util.batch_norm_for_conv2d(net, tf.constant(True), bn_decay=True, scope='bn3')
        net = tf.nn.relu(net)
        # # print(net.shape)

        conv4_w = tf.get_variable("conv4_w", [1, 1, 64, 128], initializer=tf.compat.v1.keras.initializers.glorot_normal())
        net = tf.nn.conv2d(net, conv4_w, [1, 1, 1, 1], "VALID")
        net = tf_util.batch_norm_for_conv2d(net, tf.constant(True), bn_decay=True, scope='bn4')
        net = tf.nn.relu(net)
        # print(net.shape)

        conv5_w = tf.get_variable("conv5_w", [1, 1, 128, 1024], initializer=tf.compat.v1.keras.initializers.glorot_normal())
        net = tf.nn.conv2d(net, conv5_w, [1, 1, 1, 1], "VALID")
        net = tf_util.batch_norm_for_conv2d(net, tf.constant(True), bn_decay=True, scope='bn5')
        net = tf.nn.relu(net)
        # print(net.shape)

        max_pool_2d = tf.keras.layers.MaxPooling2D(pool_size=(2 * neighbor_num + 1, 1), strides=(1, 1), padding="VALID", data_format="channels_last")
        max_pool_2d(net)
        # net = tf.nn.max_pool(net, [item_num, 2 * k + 1, 1, 1], [1, 1, 1, 1], padding='VALID')
        # print(net.shape)

        net = tf.reshape(net, [item_num, item_point_num, -1])
        # print(net.shape)

        fc1_w = tf.get_variable("fc1_w", [1024, 512], initializer=tf.compat.v1.keras.initializers.glorot_normal())
        net = tf.matmul(net, fc1_w)
        net = tf_util.batch_norm_for_fc(net, tf.constant(True), bn_decay=True, scope='bn6')
        net = tf.nn.relu(net)
        # print(net.shape)

        net = tf.nn.dropout(net, 0.7)
        fc2_w = tf.get_variable("fc2_w", [512, 256], initializer=tf.compat.v1.keras.initializers.glorot_normal())
        net = tf.matmul(net, fc2_w)
        net = tf_util.batch_norm_for_fc(net, tf.constant(True), bn_decay=True, scope='bn7')
        net = tf.nn.relu(net)
        # print(net.shape)

        net = tf.nn.dropout(net, 0.7)
        fc3_w = tf.get_variable("fc3_w", [256, 1], initializer=tf.compat.v1.keras.initializers.glorot_normal())
        net = tf.matmul(net, fc3_w)
        net = tf_util.batch_norm_for_fc(net, tf.constant(True), bn_decay=True, scope='bn8')
        net = tf.nn.relu(net)
        sess1 = tf.Session(graph=g)
        init = tf.global_variables_initializer()
        sess1.run(init)

        saver = tf.train.Saver()
        save_path = saver.save(sess1, os.path.join(LOG_DIR, "sample.ckpt"))

    with tf.Graph().as_default():
        with tf.device('/gpu:'+str(GPU_INDEX)):
            pointclouds_pl, labels_pl = MODEL.placeholder_inputs(BATCH_SIZE, NUM_POINT)
            is_training_pl = tf.placeholder(tf.bool, shape=())
            print(is_training_pl)
            
            # Note the global_step=batch parameter to minimize. 
            # That tells the optimizer to helpfully increment the 'batch' parameter for you every time it trains.
            batch = tf.Variable(0)
            bn_decay = get_bn_decay(batch)
            tf.summary.scalar('bn_decay', bn_decay)

            # Get model and loss 
            pred, end_points = MODEL.get_model(pointclouds_pl, is_training_pl, bn_decay=bn_decay)
            loss = MODEL.get_loss(pred, labels_pl, end_points)
            tf.summary.scalar('loss', loss)

            correct = tf.equal(tf.argmax(pred, 1), tf.to_int64(labels_pl))
            accuracy = tf.reduce_sum(tf.cast(correct, tf.float32)) / float(BATCH_SIZE)
            tf.summary.scalar('accuracy', accuracy)

            # Get training operator
            learning_rate = get_learning_rate(batch)
            tf.summary.scalar('learning_rate', learning_rate)
            if OPTIMIZER == 'momentum':
                optimizer = tf.train.MomentumOptimizer(learning_rate, momentum=MOMENTUM)
            elif OPTIMIZER == 'adam':
                optimizer = tf.train.AdamOptimizer(learning_rate)
            train_op = optimizer.minimize(loss, global_step=batch)
            
            # Add ops to save and restore all the variables.
            saver = tf.train.Saver()
            
        # Create a session
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        config.allow_soft_placement = True
        config.log_device_placement = False
        sess = tf.Session(config=config)

        # Add summary writers
        #merged = tf.merge_all_summaries()
        merged = tf.summary.merge_all()
        train_writer = tf.summary.FileWriter(os.path.join(LOG_DIR, 'train'),
                                  sess.graph)
        test_writer = tf.summary.FileWriter(os.path.join(LOG_DIR, 'test'))

        # Init variables
        init = tf.global_variables_initializer()
        # To fix the bug introduced in TF 0.12.1 as in
        # http://stackoverflow.com/questions/41543774/invalidargumenterror-for-tensor-bool-tensorflow-0-12-1
        #sess.run(init)
        sess.run(init, {is_training_pl: True})

        ops = {'pointclouds_pl': pointclouds_pl,
               'labels_pl': labels_pl,
               'is_training_pl': is_training_pl,
               'pred': pred,#
               'loss': loss,#
               'train_op': train_op,#
               'merged': merged,#
               'step': batch,
               'x_in': x_in,
               'net': net}#

        for epoch in range(MAX_EPOCH):
            log_string(time.asctime(time.localtime(time.time())))
            log_string('**** EPOCH %03d ****' % (epoch))
            sys.stdout.flush()
             
            train_one_epoch([sess, sess1], ops, train_writer, epoch)
            eval_one_epoch([sess, sess1], ops, test_writer, epoch)
            
            # Save the variables to disk.
            if epoch % 10 == 0:
                save_path = saver.save(sess, os.path.join(LOG_DIR, "model.ckpt"))
                log_string("Model saved in file: %s" % save_path)



def train_one_epoch(sess, ops, train_writer, epoch):
    """ ops: dict mapping from string to tf ops """
    is_training = True
    
    # Shuffle train files
    train_file_idxs = np.arange(0, len(TRAIN_FILES))
    # np.random.shuffle(train_file_idxs)
    
    for fn in range(len(TRAIN_FILES)):
        log_string('----' + str(fn) + '-----')
        current_data, current_label = provider.loadDataFile(TRAIN_FILES[train_file_idxs[fn]])
        temp_data = np.zeros((len(current_data), NUM_POINT, 3))
        if SAMPLING == 'none':
            for item in range(len(current_data)):
                the_first_n_visu(current_data[item], NUM_POINT, item)
            temp_data = current_data[:,0:NUM_POINT,:]
        elif SAMPLING == 'fps':
            for item in range(len(current_data)):
                temp_data[item] = farthest_point_sample(current_data[item], NUM_POINT, item)
            print('FPS Completed')
        elif SAMPLING == 'mine':
            for item in range(len(current_data)):
                temp_data[item] = my_point_sample(current_data[item], NUM_POINT, item)
            print('MINE Completed')
        elif SAMPLING == 'mine_neighbor':
            for item in range(len(current_data)):
                dist = distance.squareform(distance.pdist(current_data[item]))
                temp_data[item] = my_point_sample_neighbor(current_data[item], NUM_POINT, item, 128, dist)
            print('MINE NEIGHBOR Completed')
        elif SAMPLING == 'featured':
            item_num = len(current_data)
            batch_num = item_num
            temp_data = np.empty([0, NUM_POINT, 3], float)
            for i in range(batch_num):
                start_idx = i * (item_num // batch_num)
                end_idx = (i + 1) * (item_num // batch_num)
                temp = my_point_sample_featured(sess[1], ops, str(epoch) + "_" + str(fn) + "_" + str(i), current_data[start_idx : end_idx], NUM_POINT, 8)
                # temp = np.reshape(temp, [NUM_POINT, 3])
                temp_data = np.append(temp_data, temp, axis=0)
                # print("load: " + str(epoch) + "_" + str(fn) + "_" + str(i))
            print('MINE FEATURED Completed')
        current_data, current_label, _ = provider.shuffle_data(temp_data, np.squeeze(current_label))            
        current_label = np.squeeze(current_label)
        
        file_size = current_data.shape[0]
        num_batches = file_size // BATCH_SIZE
        
        total_correct = 0
        total_seen = 0
        loss_sum = 0
       
        for batch_idx in range(num_batches):
            start_idx = batch_idx * BATCH_SIZE
            end_idx = (batch_idx+1) * BATCH_SIZE
            
            # Augment batched point clouds by rotation and jittering
            rotated_data = provider.rotate_point_cloud(current_data[start_idx:end_idx, :, :])
            jittered_data = provider.jitter_point_cloud(rotated_data)
            feed_dict = {ops['pointclouds_pl']: jittered_data,
                         ops['labels_pl']: current_label[start_idx:end_idx],
                         ops['is_training_pl']: is_training,}
            summary, step, _, loss_val, pred_val = sess[0].run([ops['merged'], ops['step'],
                ops['train_op'], ops['loss'], ops['pred']], feed_dict=feed_dict)
            train_writer.add_summary(summary, step)
            pred_val = np.argmax(pred_val, 1)
            correct = np.sum(pred_val == current_label[start_idx:end_idx])
            total_correct += correct
            total_seen += BATCH_SIZE
            loss_sum += loss_val
        
        log_string('mean loss: %f' % (loss_sum / float(num_batches)))
        log_string('accuracy: %f' % (total_correct / float(total_seen)))

        
def eval_one_epoch(sess, ops, test_writer, epoch):
    """ ops: dict mapping from string to tf ops """
    is_training = False
    total_correct = 0
    total_seen = 0
    loss_sum = 0
    total_seen_class = [0 for _ in range(NUM_CLASSES)]
    total_correct_class = [0 for _ in range(NUM_CLASSES)]
    
    for fn in range(len(TEST_FILES)):
        log_string('----' + str(fn) + '-----')
        current_data, current_label = provider.loadDataFile(TEST_FILES[fn])
        temp_data = np.zeros((len(current_data), NUM_POINT, 3))
        if SAMPLING == 'none':
            temp_data = current_data[:,0:NUM_POINT,:]
        elif SAMPLING == 'fps':
            for item in range(len(current_data)):
                temp_data[item] = farthest_point_sample(current_data[item], NUM_POINT, -1)
            print('FPS Completed')
        elif SAMPLING == 'mine':
            for item in range(len(current_data)):
                temp_data[item] = my_point_sample(current_data[item], NUM_POINT, -1)
            print('MINE Completed')
        elif SAMPLING == 'mine_neighbor':
            for item in range(len(current_data)):
                dist = distance.squareform(distance.pdist(current_data[item]))
                temp_data[item] = my_point_sample_neighbor(current_data[item], NUM_POINT, item, 128, dist)
            print('MINE NEIGHBOR Completed')
        elif SAMPLING == 'featured':
            item_num = len(current_data)
            batch_num = item_num
            temp_data = np.empty([0, NUM_POINT, 3], float)
            for i in range(batch_num):
                start_idx = i * (item_num // batch_num)
                end_idx = (i + 1) * (item_num // batch_num)
                temp = my_point_sample_featured(sess[1], ops, "eval_one" + "_" + str(fn) + "_" + str(i), current_data[start_idx : end_idx], NUM_POINT, 8)
                # temp = np.reshape(temp, [NUM_POINT, 3])
                temp_data = np.append(temp_data, temp, axis=0)
            print('MINE FEATURED Completed')
        current_label = np.squeeze(current_label)
        
        file_size = temp_data.shape[0]
        num_batches = file_size // BATCH_SIZE
        
        for batch_idx in range(num_batches):
            start_idx = batch_idx * BATCH_SIZE
            end_idx = (batch_idx+1) * BATCH_SIZE

            feed_dict = {ops['pointclouds_pl']: temp_data[start_idx:end_idx, :, :],
                         ops['labels_pl']: current_label[start_idx:end_idx],
                         ops['is_training_pl']: is_training}
            summary, step, loss_val, pred_val = sess[0].run([ops['merged'], ops['step'],
                ops['loss'], ops['pred']], feed_dict=feed_dict)
            pred_val = np.argmax(pred_val, 1)
            correct = np.sum(pred_val == current_label[start_idx:end_idx])
            total_correct += correct
            total_seen += BATCH_SIZE
            loss_sum += (loss_val*BATCH_SIZE)
            for i in range(start_idx, end_idx):
                l = current_label[i]
                total_seen_class[l] += 1
                total_correct_class[l] += (pred_val[i-start_idx] == l)
            
    log_string('eval mean loss: %f' % (loss_sum / float(total_seen)))
    log_string('eval accuracy: %f'% (total_correct / float(total_seen)))
    log_string('eval avg class acc: %f' % (np.mean(np.array(total_correct_class)/np.array(total_seen_class,dtype=float))))
         


if __name__ == "__main__":
    train()
    LOG_FOUT.close()
