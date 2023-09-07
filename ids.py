# \file    mainwindow.py
# \author  IDS Imaging Development Systems GmbH
# \date    2022-06-01
# \since   1.2.0
#
# \version 1.3.0
#
# Copyright (C) 2021 - 2023, IDS Imaging Development Systems GmbH.
#
# The information in this document is subject to change without notice
# and should not be construed as a commitment by IDS Imaging Development Systems GmbH.
# IDS Imaging Development Systems GmbH does not assume any responsibility for any errors
# that may appear in this document.
#
# This document, or source code, is provided solely as an example of how to utilize
# IDS Imaging Development Systems GmbH software libraries in a sample application.
# IDS Imaging Development Systems GmbH does not assume any responsibility
# for the use or reliability of any portion of this document.
#
# General permission to copy or modify is hereby granted.

import cv2
import sys
import math
from PySide6.QtWidgets import QApplication

try:
    from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QLabel, QMainWindow, QMessageBox, QWidget, QGraphicsView, QGraphicsScene
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtCore import Qt, Slot, QTimer, QRectF
except ImportError:
    from PySide2.QtWidgets import QHBoxLayout, QVBoxLayout, QLabel, QMainWindow, QMessageBox, QWidget
    from PySide2.QtGui import QImage, QPainter
    from PySide2.QtCore import Qt, Slot, QTimer, QRectF

from ids_peak import ids_peak
from ids_peak_ipl import ids_peak_ipl
from ids_peak import ids_peak_ipl_extension


class Display(QGraphicsView):
    def __init__(self, parent: QWidget = None):
        super().__init__(parent)
        self.__scene = CustomGraphicsScene(self)
        self.setScene(self.__scene)

    @Slot(QImage)
    def on_image_received(self, image: QImage):
        self.__scene.set_image(image)
        self.update()


class CustomGraphicsScene(QGraphicsScene):
    def __init__(self, parent: Display = None):
        super().__init__(parent)
        self.__parent = parent
        self.__image = QImage()

    def set_image(self, image: QImage):
        self.__image = image
        self.update()

    def drawBackground(self, painter: QPainter, rect: QRectF):
        # Display size
        display_width = self.__parent.width()
        display_height = self.__parent.height()

        # Image size
        image_width = self.__image.width()
        image_height = self.__image.height()

        # Return if we don't have an image yet
        if image_width == 0 or image_height == 0:
            return

        # Calculate aspect ratio of display
        ratio1 = display_width / display_height
        # Calculate aspect ratio of image
        ratio2 = image_width / image_height

        if ratio1 > ratio2:
            # The height with must fit to the display height.So h remains and w must be scaled down
            image_width = display_height * ratio2
            image_height = display_height
        else:
            # The image with must fit to the display width. So w remains and h must be scaled down
            image_width = display_width
            image_height = display_height / ratio2

        image_pos_x = -1.0 * (image_width / 2.0)
        image_pox_y = -1.0 * (image_height / 2.0)

        # Remove digits after point
        image_pos_x = math.trunc(image_pos_x)
        image_pox_y = math.trunc(image_pox_y)

        rect = QRectF(image_pos_x, image_pox_y, image_width, image_height)

        painter.drawImage(rect, self.__image)



VERSION = "1.2.0"
FPS_LIMIT = 30


class MainWindow(QMainWindow):
    def __init__(self, parent: QWidget = None):
        super().__init__(parent)

        self.widget = QWidget(self)
        self.__layout = QVBoxLayout()
        self.widget.setLayout(self.__layout)
        self.setCentralWidget(self.widget)

        self.__device = None
        self.__nodemap_remote_device = None
        self.__datastream = None

        self.__display = None
        self.__acquisition_timer = QTimer()
        self.__frame_counter = 0
        self.__error_counter = 0
        self.__acquisition_running = False

        self.__label_infos = None
        self.__label_version = None
        self.__label_aboutqt = None



        self.prevImage = None

        # initialize peak library
        ids_peak.Library.Initialize()

        if self.__open_device():
            try:
                # Create a display for the camera image
                self.__display = Display()
                self.__layout.addWidget(self.__display)
                if not self.__start_acquisition():
                    QMessageBox.critical(self, "Unable to start acquisition!", QMessageBox.Ok)
            except Exception as e:
                QMessageBox.critical(self, "Exception", str(e), QMessageBox.Ok)

        else:
            self.__destroy_all()
            sys.exit(0)

        self.__create_statusbar()

        self.setMinimumSize(700, 500)
        
    def __del__(self):
        self.__destroy_all()

    def __destroy_all(self):
        # Stop acquisition
        self.__stop_acquisition()

        # Close device and peak library
        self.__close_device()
        ids_peak.Library.Close()

    def __open_device(self):
        try:
            # Create instance of the device manager
            device_manager = ids_peak.DeviceManager.Instance()

            # Update the device manager
            device_manager.Update()

            # Return if no device was found
            if device_manager.Devices().empty():
                QMessageBox.critical(self, "Error", "No device found!", QMessageBox.Ok)
                return False

            # Open the first openable device in the managers device list
            for device in device_manager.Devices():
                if device.IsOpenable():
                    self.__device = device.OpenDevice(ids_peak.DeviceAccessType_Control)
                    break

            # Return if no device could be opened
            if self.__device is None:
                QMessageBox.critical(self, "Error", "Device could not be opened!", QMessageBox.Ok)
                return False

            # Open standard data stream
            datastreams = self.__device.DataStreams()
            if datastreams.empty():
                QMessageBox.critical(self, "Error", "Device has no DataStream!", QMessageBox.Ok)
                self.__device = None
                return False

            self.__datastream = datastreams[0].OpenDataStream()

            # Get nodemap of the remote device for all accesses to the genicam nodemap tree
            self.__nodemap_remote_device = self.__device.RemoteDevice().NodeMaps()[0]

            # To prepare for untriggered continuous image acquisition, load the default user set if available and
            # wait until execution is finished
            try:
                self.__nodemap_remote_device.FindNode("UserSetSelector").SetCurrentEntry("Default")
                self.__nodemap_remote_device.FindNode("UserSetLoad").Execute()
                self.__nodemap_remote_device.FindNode("UserSetLoad").WaitUntilDone()
            except ids_peak.Exception:
                # Userset is not available
                pass

            # Get the payload size for correct buffer allocation
            payload_size = self.__nodemap_remote_device.FindNode("PayloadSize").Value()

            # Get minimum number of buffers that must be announced
            buffer_count_max = self.__datastream.NumBuffersAnnouncedMinRequired()

            # Allocate and announce image buffers and queue them
            for i in range(buffer_count_max):
                buffer = self.__datastream.AllocAndAnnounceBuffer(payload_size)
                self.__datastream.QueueBuffer(buffer)

            return True
        except ids_peak.Exception as e:
            QMessageBox.critical(self, "Exception", str(e), QMessageBox.Ok)

        return False

    def __close_device(self):
        """
        Stop acquisition if still running and close datastream and nodemap of the device
        """
        # Stop Acquisition in case it is still running
        self.__stop_acquisition()

        # If a datastream has been opened, try to revoke its image buffers
        if self.__datastream is not None:
            try:
                for buffer in self.__datastream.AnnouncedBuffers():
                    self.__datastream.RevokeBuffer(buffer)
            except Exception as e:
                QMessageBox.information(self, "Exception", str(e), QMessageBox.Ok)

    def __start_acquisition(self):
        """
        Start Acquisition on camera and start the acquisition timer to receive and display images

        :return: True/False if acquisition start was successful
        """
        # Check that a device is opened and that the acquisition is NOT running. If not, return.
        if self.__device is None:
            return False
        if self.__acquisition_running is True:
            return True

        # Get the maximum framerate possible, limit it to the configured FPS_LIMIT. If the limit can't be reached, set
        # acquisition interval to the maximum possible framerate
        try:
            max_fps = self.__nodemap_remote_device.FindNode("AcquisitionFrameRate").Maximum()
            target_fps = min(max_fps, FPS_LIMIT)
            self.__nodemap_remote_device.FindNode("AcquisitionFrameRate").SetValue(target_fps)
        except ids_peak.Exception:
            # AcquisitionFrameRate is not available. Unable to limit fps. Print warning and continue on.
            QMessageBox.warning(self, "Warning",
                                "Unable to limit fps, since the AcquisitionFrameRate Node is"
                                " not supported by the connected camera. Program will continue without limit.")

        # Setup acquisition timer accordingly
        self.__acquisition_timer.setInterval((1 / target_fps) * 1000)
        self.__acquisition_timer.setSingleShot(False)
        self.__acquisition_timer.timeout.connect(self.on_acquisition_timer)

        try:
            # Lock critical features to prevent them from changing during acquisition
            self.__nodemap_remote_device.FindNode("TLParamsLocked").SetValue(1)

            # Start acquisition on camera
            self.__datastream.StartAcquisition()
            self.__nodemap_remote_device.FindNode("AcquisitionStart").Execute()
            self.__nodemap_remote_device.FindNode("AcquisitionStart").WaitUntilDone()
        except Exception as e:
            print("Exception: " + str(e))
            return False

        # Start acquisition timer
        self.__acquisition_timer.start()
        self.__acquisition_running = True

        return True

    def __stop_acquisition(self):
        """
        Stop acquisition timer and stop acquisition on camera
        :return:
        """
        # Check that a device is opened and that the acquisition is running. If not, return.
        if self.__device is None or self.__acquisition_running is False:
            return

        # Otherwise try to stop acquisition
        try:
            remote_nodemap = self.__device.RemoteDevice().NodeMaps()[0]
            remote_nodemap.FindNode("AcquisitionStop").Execute()

            # Stop and flush datastream
            self.__datastream.KillWait()
            self.__datastream.StopAcquisition(ids_peak.AcquisitionStopMode_Default)
            self.__datastream.Flush(ids_peak.DataStreamFlushMode_DiscardAll)

            self.__acquisition_running = False

            # Unlock parameters after acquisition stop
            if self.__nodemap_remote_device is not None:
                try:
                    self.__nodemap_remote_device.FindNode("TLParamsLocked").SetValue(0)
                except Exception as e:
                    QMessageBox.information(self, "Exception", str(e), QMessageBox.Ok)

        except Exception as e:
            QMessageBox.information(self, "Exception", str(e), QMessageBox.Ok)

    def __create_statusbar(self):
        status_bar = QWidget(self.centralWidget())
        status_bar_layout = QHBoxLayout()
        status_bar_layout.setContentsMargins(0, 0, 0, 0)

        self.__label_infos = QLabel(status_bar)
        self.__label_infos.setAlignment(Qt.AlignLeft)
        status_bar_layout.addWidget(self.__label_infos)
        status_bar_layout.addStretch()

        self.__label_version = QLabel(status_bar)
        self.__label_version.setText("simple_live_qtwidgets v" + VERSION)
        self.__label_version.setAlignment(Qt.AlignRight)
        status_bar_layout.addWidget(self.__label_version)

        self.__label_aboutqt = QLabel(status_bar)
        self.__label_aboutqt.setObjectName("aboutQt")
        self.__label_aboutqt.setText("<a href='#aboutQt'>About Qt</a>")
        self.__label_aboutqt.setAlignment(Qt.AlignRight)
        self.__label_aboutqt.linkActivated.connect(self.on_aboutqt_link_activated)
        status_bar_layout.addWidget(self.__label_aboutqt)
        status_bar.setLayout(status_bar_layout)

        self.__layout.addWidget(status_bar)

    def update_counters(self):
        """
        This function gets called when the frame and error counters have changed
        :return:
        """
        self.__label_infos.setText("Acquired: " + str(self.__frame_counter) + ", Errors: " + str(self.__error_counter))

    @Slot()
    def on_acquisition_timer(self):
        """
        This function gets called on every timeout of the acquisition timer
        """
        try:
            # Get buffer from device's datastream
            buffer = self.__datastream.WaitForFinishedBuffer(5000)

            # Create IDS peak IPL image for debayering and convert it to RGBa8 format
            ipl_image = ids_peak_ipl_extension.BufferToImage(buffer)
            converted_ipl_image = ipl_image.ConvertTo(ids_peak_ipl.PixelFormatName_BGRa8)

            # Queue buffer so that it can be used again
            self.__datastream.QueueBuffer(buffer)

            # Get raw image data from converted image and construct a QImage from it
            image_np_array = converted_ipl_image.get_numpy_1D()
            image = QImage(image_np_array,
                           converted_ipl_image.Width(), converted_ipl_image.Height(),
                           QImage.Format_RGB32)
            
            
            
            img_bgr = cv2.cvtColor(image_np_array, cv2.COLOR_RGB2BGR)
            
            resized_img = cv2.resize(img_bgr, (500, 35), interpolation=cv2.INTER_AREA)
            # print("Image shape:", resized_img.shape)
            
            # resized_img = cv2.resize(img_bgr, None, fx=200, fy=300, interpolation=cv2.INTER_LINEAR)
            
            if self.prevImage is not None:
                object_changed = self.compare_object_change(self.prevImage, resized_img)
                if object_changed:
                    print("Object changed in the frame")
            
            self.prevImage = resized_img
            
            
            # Make an extra copy of the QImage to make sure that memory is copied and can't get overwritten later on
            image_cpy = image.copy()

            # Emit signal that the image is ready to be displayed
            self.__display.on_image_received(image_cpy)
            self.__display.update()

            # Increase frame counter
            self.__frame_counter += 1
        except ids_peak.Exception as e:
            self.__error_counter += 1
            print("Exception: " + str(e))

        # Update counters
        self.update_counters()

    @Slot(str)
    def on_aboutqt_link_activated(self, link):
        if link == "#aboutQt":
            QMessageBox.aboutQt(self, "About Qt")
    
    
    def compare_object_change(self, img1, img2, threshold=0.7, min_matches=10):
        orb = cv2.KAZE_create()
        # orb = cv2.ORB_create()
        
        keypoints1, descriptors1 = orb.detectAndCompute(img1, None)
        keypoints2, descriptors2 = orb.detectAndCompute(img2, None)
        
        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        matches = bf.match(descriptors1, descriptors2)
        matches = sorted(matches, key=lambda x: x.distance)
        
        # good_matches = [m for m in matches if m.distance < threshold * m.distance]
        good_matches = matches[:80]
        # print(len(good_matches), matches)
        
        7
        return len(good_matches) > min_matches

    
            
            
def main():
    a = QApplication(sys.argv)
    w = MainWindow()
    w.show()

    try:
        return a.exec()
    except AttributeError:
        return a.exec_()


if __name__ == "__main__":
    sys.exit(main())
