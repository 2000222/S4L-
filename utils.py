"""Utility functions for semi-supervised learning."""

# Python package imports
import os
import numpy as np
import scipy
from scipy import ndimage
from sklearn.metrics import accuracy_score
import tensorflow as tf
# Keras package imports
from keras.models import Model
from keras.layers import GlobalAveragePooling2D
from keras.layers import Dropout, Dense, Input, Activation
from keras import optimizers
from keras.regularizers import l2
from keras import backend as K
from keras import initializers
from keras.callbacks import Callback
from keras.utils import to_categorical

# Set seed number for reproducible randomness.
seed_number = 1
np.random.seed(seed_number)

weight_decay = 0.0005
initer = initializers.glorot_uniform(seed=seed_number)

fc_params = dict(
        activation='softmax',
        kernel_initializer=initer,
        kernel_regularizer=l2(weight_decay),
        use_bias=True,
    )


def geometric_transform(image, proxy_labels=6):
    images, labels = [], []
    image = np.reshape(image, (32, 32, 3))
    for i in range(proxy_labels):
        if i <= 3:
            t = np.rot90(image, i)
        elif i == 4:
            t = np.fliplr(image)
        else:
            t = np.flipud(image)
        images.append(t)
        labels.append(to_categorical(i, proxy_labels))
    return images, labels
        
        
def global_contrast_normalize(images, scale=55, eps=1e-10):
    images = images.astype('float32')
    n, h, w, c = images.shape
    # Flatten images to shape=(nb_images, nb_features)
    images = images.reshape((n, h*w*c))
    # Subtract out the mean of each image
    images -= images.mean(axis=1, keepdims=True)
    # Divide out the norm of each image
    per_image_norm = np.linalg.norm(images, axis=1, keepdims=True)
    # Avoid divide-by-zero
    per_image_norm[per_image_norm < eps] = 1.0
    return float(scale) * images / per_image_norm


def zca_whitener(images, identity_scale=0.1, eps=1e-10):
    """Args:
        images: array of flattened images, shape=(n_images, n_features)
        identity_scale: scalar multiplier for identity in SVD
        eps: small constant to avoid divide-by-zero
    Returns:
        A function which applies ZCA to an array of flattened images
    """
    image_covariance = np.cov(images, rowvar=False)
    U, S, _ = np.linalg.svd(
        image_covariance + identity_scale * np.eye(*image_covariance.shape)
    )
    zca_decomp = np.dot(U, np.dot(np.diag(1. / np.sqrt(S + eps)), U.T))
    image_mean = images.mean(axis=0)
    return lambda x: np.dot(x - image_mean, zca_decomp)


def stratified_sample(label_array, labels_per_class):
    samples = []
    for cls in range(len(set(label_array))):
        inds = np.where(label_array == cls)[0]
        np.random.shuffle(inds)
        inds = inds[:labels_per_class].tolist()
        samples.extend(inds)
    return samples


def gaussian_noise(image, stddev=0.15):
    return image + np.random.randn(*image.shape) * stddev


def transform_matrix_offset_center(matrix, x, y):
    o_x = float(x) / 2 + 0.5
    o_y = float(y) / 2 + 0.5
    offset_matrix = np.array([[1, 0, o_x], [0, 1, o_y], [0, 0, 1]])
    reset_matrix = np.array([[1, 0, -o_x], [0, 1, -o_y], [0, 0, 1]])
    transform_matrix = np.dot(np.dot(offset_matrix, matrix), reset_matrix)
    return transform_matrix


def jitter(image, row_axis=0, col_axis=1, channel_axis=2,
           fill_mode='reflect', cval=0.0, order=1):
    tx = np.random.choice([1, 2])
    tx *= np.random.choice([-1, 1])
    ty = np.random.choice([1, 2])
    ty *= np.random.choice([-1, 1])
    
    transform_matrix = np.array([[1, 0, tx],
                                 [0, 1, ty],
                                 [0, 0, 1]])
    h, w = image.shape[row_axis], image.shape[col_axis]
    transform_matrix = transform_matrix_offset_center(
            transform_matrix, h, w)
    image = np.rollaxis(image, channel_axis, 0)
    final_affine_matrix = transform_matrix[:2, :2]
    final_offset = transform_matrix[:2, 2]

    channel_images = [ndimage.interpolation.affine_transform(
        image_channel,
        final_affine_matrix,
        final_offset,
        order=order,
        mode=fill_mode,
        cval=cval) for image_channel in image]
    image = np.stack(channel_images, axis=0)
    image = np.rollaxis(image, 0, channel_axis + 1)
    return image


def datagen(super_iter, self_iter, batch_size):
    """Utility function to load data into required Keras model format."""
    super_batch = 192
    self_batch = batch_size
    while(True):
        x_super, y_super = zip(*[next(super_iter) for _ in range(super_batch)])
        x_self, y_self = zip(*[geometric_transform(next(self_iter))
                               for _ in range(self_batch)])
        x_super = np.vstack(x_super)
        y_super = np.vstack(y_super)
        x_self = np.vstack(x_self)
        y_self = np.vstack(y_self)
        yield ([x_self, x_super], [y_self, y_super])


def datagen_tinyimages(super_iter, self_iter, extra_iter, batch_size):
    """Function to load extra tiny images into required Keras model format."""
    super_batch = 192
    self_batch = batch_size
    extra_batch = 32 - batch_size # self_batch + extra_batch = 32
    inds = np.arange(super_batch)
    while(True):
        x_super, y_super = zip(*[next(super_iter) for _ in range(super_batch)])
        x_self, y_self = zip(*[geometric_transform(next(self_iter))
                               for _ in range(self_batch)])
        x_extra, y_extra = zip(*[geometric_transform(next(extra_iter))
                                 for _ in range(extra_batch)])
        x_super = np.vstack(x_super)
        y_super = np.vstack(y_super)
        x_self = np.vstack(x_self + x_extra)
        y_self = np.vstack(y_self + y_extra)
        # Shuffle in batch.
        np.random.shuffle(inds)
        x_self = x_self[inds]
        y_self = y_self[inds]
        yield ([x_self, x_super], [y_self, y_super])


def load_tinyimages(indices):
    dirname = './datasets/tiny-images'
    fpath = os.path.join(dirname, 'tiny_images.bin')
    images = np.zeros((len(indices), 3, 32, 32), dtype='float32')
    with open(fpath, 'rb') as f:
        for i, idx in enumerate(indices):
            f.seek(3072 * idx)
            image = np.fromfile(f, dtype='uint8', count=3072)
            images[i] = np.reshape(image, (3, 32, 32))
    images = np.transpose(images, (0, 3, 2, 1)) / 255.
    return images
	
# define the vat-loss for semi-supervised learning
'''def compute_kld(p_logit, q_logit):
    p = tf.nn.softmax(p_logit)
    q = tf.nn.softmax(q_logit)
    return tf.reduce_sum(p*(tf.log(p + 1e-16) - tf.log(q + 1e-16)), axis=1)

def make_unit_norm(x):
    return x/(tf.reshape(tf.sqrt(tf.reduce_sum(tf.pow(x, 2.0), axis=1)), [-1, 1]) + 1e-16)

def vat_loss(cnn_trunk,input_shape,self_out,self_in):
    #data = Input(shape=input_shape)
    #p_logit = model.create_model(data)
    p_logit = self_out
    p = Activation('softmax')(p_logit)
    
    r = tf.random_normal(shape=tf.shape(self_in))
    r = make_unit_norm(r)
    p_logit_r = cnn_trunk(self_in+10*r)

    kl = tf.reduce_mean(compute_kld(p_logit,p_logit_r))
    grad_kl = tf.gradients(kl,[r])[0]
    r_vadv = tf.stop_gradient(grad_kl)
    r_vadv = make_unit_norm(r_vadv)/3.0

    p_logit_no_gradient = tf.stop_gradient(p_logit)
    p_logit_r_adv = cnn_trunk(self_out+ r_vadv)
    vat_loss1 = tf.reduce_mean(compute_kld(p_logit_no_gradient, p_logit_r_adv))
    return vat_loss1'''
			
def open_sesemi(model, input_shape, nb_classes, lrate, dropout):
    cnn_trunk = model.create_model(input_shape)
    #resnet50_trunk = model.create_model(input_shape)
	
    super_in = Input(shape=input_shape, name='super_data')
    self_in = Input(shape=input_shape, name='self_data')
    super_out = cnn_trunk(super_in)
    self_out = cnn_trunk(self_in)
    
    super_out = GlobalAveragePooling2D(name='super_gap')(super_out)
    if dropout > 0.0:
        super_out = Dropout(dropout, name='dropout')(super_out)
    self_out = GlobalAveragePooling2D(name='self_gap')(self_out)
    
    super_out = Dense(nb_classes, name='super_clf', **fc_params)(super_out)
    self_out = Dense(6, name='self_clf', **fc_params)(self_out)

    sesemi_model = Model(inputs=[self_in, super_in],
                         outputs=[self_out, super_out])
    inference_model = Model(inputs=[super_in], outputs=[super_out])
    #vat_loss1 = vat_loss(cnn_trunk,input_shape,self_out,self_in)
    #sesemi_model.add_loss(vat_loss1)
	
    sgd = optimizers.SGD(lr=lrate, momentum=0.9, nesterov=True)
    #sesemi_model.metrics_names.append('vat_loss')
    #sesemi_model.metrics_tensors.append(vat_loss1)
	
    sesemi_model.compile(optimizer=sgd,
                         loss={'super_clf': 'categorical_crossentropy',
                               'self_clf' : 'categorical_crossentropy'},
                         loss_weights={'super_clf': 1.0, 'self_clf': 1.0},
                         metrics=None)
    return sesemi_model, inference_model


class LRScheduler(Callback):
    def __init__(self, base_lr, max_iter, power=0.5):
        self.base_lr = base_lr
        self.max_iter = float(max_iter)
        self.power = power
        self.batches = 0
        
    def on_batch_begin(self, batch, logs={}):
        lr = self.base_lr * (1.0 - (self.batches / self.max_iter)) ** self.power
        K.set_value(self.model.optimizer.lr, lr)
        self.batches += 1
        
    def on_epoch_begin(self, epoch, logs={}):
        print('Learning rate: ', K.get_value(self.model.optimizer.lr))


class DenseEvaluator(Callback):
    def __init__(self, inference_model, validation_data, hflip):
        x_val = validation_data[0]
        y_val = validation_data[1]

        self.data = []
        self.labels = y_val
        self.inference_model = inference_model
        self.hflip = hflip
        
        for x in x_val:
            t = jitter(x)
            noisy_x = gaussian_noise(x)
            noisy_t = gaussian_noise(t)
            if self.hflip:
                flipx = np.fliplr(x)
                flipt = np.fliplr(t)
                noisy_flipx = gaussian_noise(flipx)
                noisy_flipt = gaussian_noise(flipt)
                self.data.append([x, t, noisy_x, noisy_t,
                                  flipx, flipt, noisy_flipx, noisy_flipt])
            else:
                self.data.append([x, t, noisy_x, noisy_t])
        self.data = np.vstack(self.data)
        
    def on_epoch_end(self, epoch, logs={}):
        y_pred = self.inference_model.predict(self.data, batch_size=64)
        if self.hflip:
            y_pred = y_pred.reshape((len(y_pred) // 8, 8, -1))
        else:
            y_pred = y_pred.reshape((len(y_pred) // 4, 4, -1))
        y_pred = y_pred.mean(axis=1)
        y_pred = np.argmax(y_pred, axis=1)
        
        y_true = self.labels
        
        error = 1.0 - accuracy_score(y_true, y_pred)
        print('sesemi_error: {:.4f}'.format(error), '\n')

