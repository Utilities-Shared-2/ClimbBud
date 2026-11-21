
from openvino import Core
import numpy as np
import cv2

#from src import utils

import utils

class FaceDetector:
    def __init__(self,
                 model,
                 confidence_thr=0.5,
                 overlap_thr=0.7):
        # load and compile the model
        core = Core()
        model = core.read_model(model=model)
        compiled_model = core.compile_model(model=model)
        self.model = compiled_model

        # 'Cause that model has more than one output,
        # We are saving the names in a more human frendly
        # variable to remember later how to recover the output we wish
        # In our case here, is a output for hte bbox and other for the score
        # /confidence. Have a look at the openvino documentation for more i
        self.output_scores_layer = self.model.output(0)
        self.output_boxes_layer  = self.model.output(1)
        # confidence threshold
        self.confidence_thr = confidence_thr
        # threshold for the nonmaximum suppression
        self.overlap_thr = overlap_thr

    def preprocess(self, image):
        """
            input image is a numpy array image representation,
            in the BGR format of any shape.
        """
        # resize to match the expected by the model
        input_image = cv2.resize(image, dsize=[320,240])
        # changing from [H, W, C] to [C, H, W]. "channels first"
        input_image = np.expand_dims(input_image.transpose(2,0,1), axis=0)
        return input_image

    def posprocess(self, pred_scores, pred_boxes, image_shape):
        # get all predictions with more than confidence_thr of confidence
        filtered_indexes = np.argwhere( pred_scores[0,:,1] > self.confidence_thr  ).tolist()
        filtered_boxes   = pred_boxes[0,filtered_indexes,:]
        filtered_scores  = pred_scores[0,filtered_indexes,1]

        if len(filtered_scores) == 0:
            return [],[]

        # convert all boxes to image coordinates
        h, w = image_shape
        def _convert_bbox_format(*args):
            bbox = args[0]
            x_min, y_min, x_max, y_max = bbox
            x_min = int(w*x_min)
            y_min = int(h*y_min)
            x_max = int(w*x_max)
            y_max = int(h*y_max)
            return x_min, y_min, x_max, y_max

        bboxes_image_coord = np.apply_along_axis(_convert_bbox_format, axis = 2, arr=filtered_boxes)

        # apply non-maximum supressions
        bboxes_image_coord, indexes = utils.non_max_suppression(bboxes_image_coord.reshape([-1,4]),
                                                                overlapThresh=self.overlap_thr)
        filtered_scores = filtered_scores[indexes]
        return bboxes_image_coord, filtered_scores

    def draw_bboxes(self, image, bboxes, color=[0,255,0]):
        # Just for visualization
        # draw all bboxes on the input image
        for boxe in bboxes:
            x_min, y_min, x_max, y_max = boxe
            pt1 = (x_min, y_min)
            pt2 = (x_max, y_max)
            cv2.rectangle(image, pt1, pt2, color=color, thickness=2, lineType=cv2.LINE_4)#BGR

    def inference(self, image):
        input_image = self.preprocess(image)
        # inference
        pred_scores = self.model( [input_image] )[self.output_scores_layer]
        pred_boxes = self.model( [input_image] )[self.output_boxes_layer]

        image_shape = image.shape[:2]
        faces, scores = self.posprocess(pred_scores, pred_boxes, image_shape)
        return faces, scores

class PoseDetector:
    """
    YOLO11n-pose (ONNX, run with OpenVINO).
    Model output is [1, 56, N]: per candidate 4 box values (cx, cy, w, h in
    input-pixel space), 1 person score, then 17 COCO keypoints x (x, y, conf).
    """
    # COCO keypoint indexes 0-4: nose, left eye, right eye, left ear, right ear
    HEAD_KPTS = [0, 1, 2, 3, 4]
    # shoulders + hips: the most stable points for the body's centre of mass
    TORSO_KPTS = [5, 6, 11, 12]
    # COCO-17 keypoint names, in model output order
    KEYPOINT_NAMES = [
        "nose", "left_eye", "right_eye", "left_ear", "right_ear",
        "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
        "left_wrist", "right_wrist", "left_hip", "right_hip",
        "left_knee", "right_knee", "left_ankle", "right_ankle",
    ]

    def __init__(self,
                 model,
                 confidence_thr=0.5,
                 overlap_thr=0.45,
                 kpt_thr=0.5):
        core = Core()
        model = core.read_model(model=model)
        self.model = core.compile_model(model=model)
        self.output_layer = self.model.output(0)
        # input shape is [1, 3, H, W]; read it so a re-export at another imgsz still works
        _, _, self.in_h, self.in_w = [int(d) for d in self.model.input(0).shape]
        self.confidence_thr = confidence_thr
        self.overlap_thr = overlap_thr
        self.kpt_thr = kpt_thr

    def preprocess(self, image):
        """Letterbox to the model input size, BGR->RGB, scale to 0-1, HWC->NCHW."""
        h, w = image.shape[:2]
        self.scale = min(self.in_w / w, self.in_h / h)
        new_w, new_h = int(round(w * self.scale)), int(round(h * self.scale))
        self.pad_x = (self.in_w - new_w) // 2
        self.pad_y = (self.in_h - new_h) // 2

        resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((self.in_h, self.in_w, 3), 114, dtype=np.uint8)
        canvas[self.pad_y:self.pad_y + new_h, self.pad_x:self.pad_x + new_w] = resized

        blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        return np.expand_dims(blob.transpose(2, 0, 1), axis=0)

    def posprocess(self, output, image_shape):
        empty = (np.zeros((0, 4), dtype=int), np.zeros((0, 1)), np.zeros((0, 17, 3)))
        pred = output[0].T  # (N, 56)
        pred = pred[pred[:, 4] > self.confidence_thr]
        if len(pred) == 0:
            return empty

        h, w = image_shape
        cx, cy, bw, bh = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
        boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)
        # undo the letterbox
        boxes[:, [0, 2]] = (boxes[:, [0, 2]] - self.pad_x) / self.scale
        boxes[:, [1, 3]] = (boxes[:, [1, 3]] - self.pad_y) / self.scale
        boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, w - 1)
        boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, h - 1)
        scores = pred[:, 4]

        # score-ordered NMS (expects x, y, w, h)
        xywh = np.column_stack([boxes[:, 0], boxes[:, 1],
                                boxes[:, 2] - boxes[:, 0], boxes[:, 3] - boxes[:, 1]])
        keep = cv2.dnn.NMSBoxes(xywh.tolist(), scores.tolist(),
                                self.confidence_thr, self.overlap_thr)
        if len(keep) == 0:
            return empty
        keep = np.array(keep).flatten()

        kpts = pred[keep, 5:].reshape(-1, 17, 3).copy()
        kpts[:, :, 0] = (kpts[:, :, 0] - self.pad_x) / self.scale
        kpts[:, :, 1] = (kpts[:, :, 1] - self.pad_y) / self.scale

        return boxes[keep].astype(int), scores[keep].reshape(-1, 1), kpts

    def inference(self, image):
        input_image = self.preprocess(image)
        output = self.model([input_image])[self.output_layer]
        return self.posprocess(output, image.shape[:2])

    @staticmethod
    def head_point(kpts, kpt_thr=0.5):
        """Average of the visible face keypoints (nose/eyes/ears), or None if none are visible."""
        head = kpts[PoseDetector.HEAD_KPTS]
        visible = head[head[:, 2] > kpt_thr]
        if len(visible) == 0:
            return None
        return int(visible[:, 0].mean()), int(visible[:, 1].mean())

    @staticmethod
    def keypoint_positions(kpts, kpt_thr=0.5):
        """
        Position of every body vertex (keypoint) for one person.
        :param kpts: (17, 3) array of (x, y, conf) in frame pixels
        :return: {name: {"x", "y", "conf", "visible"}} for all 17 COCO keypoints.
                 Low-confidence points are kept but flagged visible=False.
        """
        return {
            name: {
                "x": int(x),
                "y": int(y),
                "conf": float(conf),
                "visible": bool(conf > kpt_thr),
            }
            for name, (x, y, conf) in zip(PoseDetector.KEYPOINT_NAMES, kpts)
        }

    @staticmethod
    def body_position(kpts, box=None, kpt_thr=0.5, image_shape=None):
        """
        General position of one person's body.
        Centre is the mean of the visible torso keypoints (shoulders/hips),
        falling back to all visible keypoints, then to the box centre.
        :param kpts: (17, 3) array of (x, y, conf) in frame pixels
        :param box: optional (x1, y1, x2, y2) person box
        :param image_shape: optional (h, w); if given, also returns the centre
                            normalised to [-1, 1] relative to the frame middle
        :return: dict with center, source, bbox, width, height, normalized;
                 or None if nothing is visible and no box was given
        """
        visible = kpts[kpts[:, 2] > kpt_thr]
        torso = kpts[PoseDetector.TORSO_KPTS]
        torso = torso[torso[:, 2] > kpt_thr]

        if len(torso):
            center, source = torso[:, :2].mean(axis=0), "torso"
        elif len(visible):
            center, source = visible[:, :2].mean(axis=0), "keypoints"
        elif box is not None:
            x1, y1, x2, y2 = box
            center, source = ((x1 + x2) / 2, (y1 + y2) / 2), "box"
        else:
            return None
        cx, cy = int(center[0]), int(center[1])

        if box is not None:
            x1, y1, x2, y2 = (int(v) for v in box)
        else:  # no box: use the extent of the visible keypoints
            x1, y1 = visible[:, :2].min(axis=0).astype(int)
            x2, y2 = visible[:, :2].max(axis=0).astype(int)

        normalized = None
        if image_shape is not None:
            h, w = image_shape[:2]
            normalized = (round((cx - w / 2) / (w / 2), 3), round((cy - h / 2) / (h / 2), 3))

        return {
            "center": (cx, cy),
            "source": source,
            "bbox": (int(x1), int(y1), int(x2), int(y2)),
            "width": int(x2 - x1),
            "height": int(y2 - y1),
            "normalized": normalized,
        }
