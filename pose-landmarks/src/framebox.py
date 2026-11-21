import numpy as np
import cv2


def draw_largest_box(image, boxes, scores):
    """
    Draw the largest face box on the image.
    :param image:
    :param boxes:
    :param scores:
    :return: image, ((x_min, y_min), (x_max, y_max)), index of the chosen box
    """
    if len(boxes) == 0:
        return image, None, None

    num_boxes = boxes.shape[0]


    #find the largest box

    #chatgpt generated this to find the index of the largest box in one line
    idx = max(range(len(boxes)), key=lambda i: (boxes[i][2]-boxes[i][0]) * (boxes[i][3]-boxes[i][1]))


    box = boxes[idx]
    score = scores[idx][0]

    # Convert the box coordinates to integers
    box = box.astype(int)

    # Draw the box on the image
    cv2.rectangle(image, (box[0], box[1]), (box[2], box[3]), (0, 255, 0), 2)

    # Define the text parameters
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.8
    color = (255, 255, 255)
    thickness = 1

    # Create the text string
    text = '{:.2f}'.format(score)

    # Determine the text size
    text_size = cv2.getTextSize(text, font, font_scale, thickness)[0]

    # Define the text position relative to the box
    text_x = box[0]
    text_y = box[1] - text_size[1]

    # Draw the text background rectangle
    cv2.rectangle(image, (text_x, text_y), (text_x + text_size[0], text_y + text_size[1]), (0, 255, 0), -1)

    # Draw the text on top of the background rectangle
    cv2.putText(image, text, (text_x, text_y + text_size[1]), font, font_scale, color, thickness)

    #returns the image and ((x_min, y_min), (x_max, y_max))
    return image, ((box[0], box[1]), (box[2], box[3])), idx




# COCO-17 skeleton: (keypoint a, keypoint b, BGR colour)
_HEAD, _ARM, _TORSO, _LEG = (0, 255, 255), (0, 165, 255), (0, 255, 0), (255, 0, 255)
SKELETON = [
    (0, 1, _HEAD), (0, 2, _HEAD), (1, 3, _HEAD), (2, 4, _HEAD),   # face
    (3, 5, _HEAD), (4, 6, _HEAD),                                 # ears -> shoulders
    (5, 7, _ARM), (7, 9, _ARM), (6, 8, _ARM), (8, 10, _ARM),      # arms
    (5, 6, _TORSO), (5, 11, _TORSO), (6, 12, _TORSO), (11, 12, _TORSO),  # torso
    (11, 13, _LEG), (13, 15, _LEG), (12, 14, _LEG), (14, 16, _LEG),      # legs
]


def draw_pose(image, kpts, kpt_thr=0.5, highlight=None):
    """
    Draw the pose landmarks and bones for every detected person.
    :param kpts: (N, 17, 3) array of (x, y, conf) per keypoint
    :param highlight: index of the tracked person, drawn thicker
    """
    for p, person in enumerate(kpts):
        thick = 2 if p == highlight else 1
        visible = person[:, 2] > kpt_thr
        pts = person[:, :2].astype(int)
        for a, b, color in SKELETON:
            if visible[a] and visible[b]:
                cv2.line(image, tuple(pts[a]), tuple(pts[b]), color, thick, cv2.LINE_AA)
        for i in np.flatnonzero(visible):
            cv2.circle(image, tuple(pts[i]), thick + 2, (255, 255, 255), -1, cv2.LINE_AA)
    return image
