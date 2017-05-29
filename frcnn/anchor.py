import sonnet as snt
import tensorflow as tf
import numpy as np

from utils.generate_anchors import generate_anchors
from utils.cython_bbox import bbox_overlaps
from utils.bbox_transform import bbox_transform


class AnchorTarget(snt.AbstractModule):
    """
    AnchorTarget
    
    TODO: (copied) Assign anchors to ground-truth targets. Produces anchor
    classification labels and bounding-box regression targets.

    Detailed responsabilities:
    - Keep anchors that are inside the image.
    - We need to set each anchor with a label:
        1 is positive
            when GT overlap is >= 0.7 or for GT max overlap (one anchor)
        0 is negative
            when GT overlap is < 0.3
        -1 is don't care
            useful for subsampling negative labels

    - Create BBox targets with anchors and GT.
        TODO:
        Cual es la diferencia entre bbox_inside_weights y bbox_outside_weights


    Things to take into account:
    - We can assign "don't care" labels to anchors we want to ignore in batch.


    Returns:
        labels: label for each anchor
        bbox_targets: bbox regresion values for earch anchor
        bbox_inside_weights: TODO: ??
        bbox_outside_weights: TODO: ??

    """
    def __init__(self, anchor_scales, anchor_ratios, feat_stride=[16],
                 name='anchor_target'):
        super(AnchorTarget, self).__init__(name=name)
        self._anchor_scales = anchor_scales
        self._anchor_ratios = anchor_ratios
        self._num_anchors = self.anchors.shape[0]
        self._feat_stride = feat_stride

        self._allowed_border = 0
        self._clobber_positives = False
        self._positive_overlap = 0.7
        self._negative_overlap = 0.3
        self._foreground_fraction = 0.5
        self._batch_size = 256
        self._bbox_inside_weights = (1.0, 1.0, 1.0, 1.0)

    def _build(self, rpn_cls_score, gt_boxes):
        """
        We currently use the `anchor_target_layer` code provided in the
        original Caffe implementation by Ross Girshick. Ideally we should
        try to migrate this code to pure Tensorflow tensor-based graph.

        TODO: Tensorflow limitations for the migration.
        TODO: Performance impact of current use of py_func
        TODO: Alternative to migrate to pure cython (overlaps is already implemented in cython)
        """

        (
            labels, bbox_targets,
            bbox_inside_weights, bbox_outside_weights
        ) = tf.py_func(
            self._anchor_target_layer_np,
            [rpn_cls_score, gt_boxes],
            [tf.float32, tf.float32, tf.float32, tf.float32]

        )

        return labels, bbox_targets, bbox_inside_weights, bbox_outside_weights

        

    def _anchor_target_layer(self, rpn_cls_score):
        """
        Function working with Tensors instead of instances for proper
        computing in the Tensorflow graph.
        """
        raise NotImplemented()


    def _anchor_target_layer_np(self, rpn_cls_score, gt_boxes):
        """
        Function to be executed with tf.py_func
        """

        height, width = rpn_cls_score.shape[1:3]

        # 1. Generate proposals from bbox deltas and shifted anchors
        shift_x = np.arange(0, width) * self._feat_stride
        shift_y = np.arange(0, height) * self._feat_stride

        shift_x, shift_y = np.meshgrid(shift_x, shift_y) # in W H order

        # K is H x W
        shifts = np.vstack(
            (shift_x.ravel(), shift_y.ravel(),
             shift_x.ravel(), shift_y.ravel())
        ).transpose()

        # add A anchors (1, A, 4) to
        # cell K shifts (K, 1, 4) to get
        # shift anchors (K, A, 4)
        # reshape to (K*A, 4) shifted anchors
        A = self._num_anchors
        K = shifts.shape[0]  # ( W * H ?)

        all_anchors = (
            self.anchors.reshape((1, A, 4)) +
            shifts.reshape((1, K, 4)).transpose((1, 0, 2)))

        all_anchors = all_anchors.reshape((K * A, 4))

        total_anchors = int(K * A)

        # only keep anchors inside the image
        inds_inside = np.where(
            (all_anchors[:, 0] >= -self._allowed_border) &
            (all_anchors[:, 1] >= -self._allowed_border) &
            (all_anchors[:, 2] < im_info[1] + self._allowed_border) &  # width
            (all_anchors[:, 3] < im_info[0] + self._allowed_border)    # height
        )[0]

        # keep only inside anchors
        anchors = all_anchors[inds_inside, :]

        labels = np.empty((len(inds_inside), ), dtype=np.float32)
        labels.fill(-1)

        # overlaps between the anchors and the gt boxes
        # overlaps (ex, gt)
        overlaps = self._bbox_overlaps(
            np.ascontiguousarray(anchors, dtype=np.float),
            np.ascontiguousarray(gt_boxes, dtype=np.float))
        argmax_overlaps = overlaps.argmax(axis=1)
        max_overlaps = overlaps[np.arange(len(inds_inside)), argmax_overlaps]
        gt_argmax_overlaps = overlaps.argmax(axis=0)
        gt_max_overlaps = overlaps[gt_argmax_overlaps,
                                   np.arange(overlaps.shape[1])]
        gt_argmax_overlaps = np.where(overlaps == gt_max_overlaps)[0]

        if not self._clobber_positives:
            # assign bg labels first so that positive labels can clobber them
            labels[max_overlaps < self._negative_overlap] = 0

        # foreground label: for each ground-truth, anchor with highest overlap
        labels[gt_argmax_overlaps] = 1

        # foreground label: above threshold Intersection over Union (IoU)
        labels[max_overlaps >= self._positive_overlap] = 1

        if self._clobber_positives:
            # assign background labels last so that negative labels can clobber positives
            labels[max_overlaps < self._negative_overlap] = 0

        # subsample positive labels if we have too many
        num_fg = int(self._foreground_fraction * self._batch_size)
        fg_inds = np.where(labels == 1)[0]
        if len(fg_inds) > num_fg:
            disable_inds = np.random.choice(
                fg_inds, size=(len(fg_inds) - num_fg), replace=False)
            labels[disable_inds] = -1

        # subsample negative labels if we have too many
        num_bg = self._batch_size - np.sum(labels == 1)
        bg_inds = np.where(labels == 0)[0]
        if len(bg_inds) > num_bg:
            disable_inds = np.random.choice(
                bg_inds, size=(len(bg_inds) - num_bg), replace=False)
            labels[disable_inds] = -1

        bbox_targets = np.zeros((len(inds_inside), 4), dtype=np.float32)
        bbox_targets = self._compute_targets(anchors, gt_boxes[argmax_overlaps, :])

        bbox_inside_weights = np.zeros((len(inds_inside), 4), dtype=np.float32)
        bbox_inside_weights[labels == 1, :] = np.array(self._bbox_inside_weights)

        bbox_outside_weights = np.zeros((len(inds_inside), 4), dtype=np.float32)

        # uniform weighting of examples (given non-uniform sampling)
        num_examples = np.sum(labels >= 0) + 1

        positive_weights = np.ones((1, 4)) * 1.0 / num_examples
        negative_weights = np.ones((1, 4)) * 1.0 / num_examples

        # in TFFRCNN is:
        #   positive_weights = np.ones((1, 4))
        #   negative_weights = np.zeros((1, 4))

        bbox_outside_weights[labels == 1, :] = positive_weights
        bbox_outside_weights[labels == 0, :] = negative_weights

        labels = _unmap(
            labels, total_anchors, inds_inside, fill=-1)
        bbox_targets = _unmap(
            bbox_targets, total_anchors, inds_inside, fill=0)
        bbox_inside_weights = _unmap(
            bbox_inside_weights, total_anchors, inds_inside, fill=0)
        bbox_outside_weights = _unmap(
            bbox_outside_weights, total_anchors, inds_inside, fill=0)

        # labels
        labels = labels.reshape((1, height, width, A)).transpose(0, 3, 1, 2)
        labels = labels.reshape((1, 1, A * height, width))

        # bbox_targets
        bbox_targets = bbox_targets.reshape(
            (1, height, width, A * 4)
        ).transpose(0, 3, 1, 2)

        # bbox_inside_weights
        bbox_inside_weights = bbox_inside_weights.reshape(
            (1, height, width, A * 4)
        ).transpose(0, 3, 1, 2)

        # bbox_outside_weights
        bbox_outside_weights = bbox_outside_weights.reshape(
            (1, height, width, A * 4)
        ).transpose(0, 3, 1, 2)

        return labels, bbox_targets, bbox_inside_weights, bbox_outside_weights

    @property
    def anchors(self):
        if not hasattr(self, '_anchors') or self._anchors is None:
            self._anchors = self._generate_anchors()
        return self._anchors

    def _generate_anchors(self):
        return generate_anchors(
            ratios=self._anchor_ratios, scales=self._anchor_scales
        )

    def _bbox_overlaps(self, boxes, gt_boxes):
        return bbox_overlaps(boxes, gt_boxes)

    def _compute_targets(self, boxes, groundtruth_boxes):
        """Compute bounding-box regression targets for an image."""

        assert boxes.shape[0] == groundtruth_boxes.shape[0]
        assert boxes.shape[1] == 4
        # TODO: Why 5?!!
        assert groundtruth_boxes.shape[1] == 5

        return bbox_transform(
            boxes, groundtruth_boxes[:, :4]).astype(np.float32, copy=False)

    def _unmap(self, data, count, inds, fill=0):
        """
        Unmap a subset of item (data) back to the original set of items (of
        size count)

        # TODO(vierja): Revisar
        """
        if len(data.shape) == 1:
            ret = np.empty((count, ), dtype=np.float32)
            ret.fill(fill)
            ret[inds] = data
        else:
            ret = np.empty((count, ) + data.shape[1:], dtype=np.float32)
            ret.fill(fill)
            ret[inds, :] = data
        return ret





















