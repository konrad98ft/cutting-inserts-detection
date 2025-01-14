from pypylon import pylon
import cv2 as cv
import numpy as np
import math
from numpy.core.fromnumeric import shape 
import imutils
import time
from scipy import ndimage
import tensorflow as tf
import tensorflow.keras as keras
from tensorflow.keras.preprocessing import image
from keras.models import model_from_json

'''
Program for embedded ADLINK NEON 2000 Smart Camera. Use Basler pylon to grab the frames.
It presents hybrid approach: compute frames using both: deep learning and classic algorithms.
Models for deep learning can be developed in the dedicated Google Colab environment.

'''

# Parameters
PX2MM = 580/4 #R = 4mm R = 620px (based on the round of the cutting insert)
MAX_DIM = 137 # Max radius of the cutting insert [px]
MIN_DIM = 128 # Min radius of the cutting insert [px]
STD_ERROR = 1.5 # Max std error for edge of the cutting insert [px]


#------------------Configuration--------------------#
# Conecting to the available camera
camera = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateFirstDevice())

# Grabing Continusely (video) with minimal delay
camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly) 
converter = pylon.ImageFormatConverter()

# Converting to opencv bgr format
converter.OutputPixelFormat = pylon.PixelType_BGR8packed
converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned

# Loading neural network model (it takes soome time)
start_time = time.time()
json_file = open("model.json", 'r')
loaded_model_json = json_file.read()
json_file.close()
model = model_from_json(loaded_model_json)
model.load_weights("model.h5")
print("Model loaded sucesfully in",time.time()-start_time,"s")
#---------------Configuration-end------------------#


#--------------------Functions---------------------#
# Clasic image processing
def linesFiltration(roi,direction):
    '''
    Apply 2d filters to the image with help to highlight straight lines.
    Kernels are chosen based of the lines' orientation.
    '''
    # Define kernels
    a = [-2,-1,1,6,1,-1,-2]
    kernel1 = np.array([[a[0],a[0],a[0],a[0],a[0],a[0],a[0]],[a[1],a[1],a[1],a[1],a[1],a[1],a[1]],[a[2],a[2],a[2],a[2],a[2],a[2],a[2]],
                       [a[3],a[3],a[3],a[3],a[3],a[3],a[3]],
                       [a[2],a[2],a[2],a[2],a[2],a[2],a[2]],[a[1],a[1],a[1],a[1],a[1],a[1],a[1]],[a[0],a[0],a[0],a[0],a[0],a[0],a[0]]])
    kernel2 = np.array([a,a,a,a,a])
    
    # Chose kernel proper for defined direction
    if(direction[1]!=0): kernel = kernel1
    if(direction[0]!=0): kernel = kernel2
    roi2 = cv.filter2D(roi,-1,kernel)
    #showResizedImg(roi2,'linesFiltration',scale = 2 )
    return roi2

def findLinesPoints(roi,direction):   
    '''
    Return coordinates of the points with belongs to the edge.
    Apply some filters to reject noise.
    '''
    
    # Some preprocessing
    kernel = np.ones((7,7),np.uint8)
    roi = cv.morphologyEx(roi, cv.MORPH_OPEN, kernel)
    roi = cv.Canny(roi,100,30)

    #showResizedImg(roi,'findLinesPoints1',scale = 2 ) 

    # Rotate image to ensure proper searching direction
    if(direction[0] == -1): roi = cv.flip(roi, 1) 
    if(direction[1] != 0): 
        roi =  cv.rotate(roi, cv.ROTATE_90_CLOCKWISE)
        if(direction[1] == -1): roi = cv.flip(roi, 1)  

    # Find min non zero val in each row
    rows,cols = roi.shape
    drawing = np.zeros((rows, cols), dtype=np.uint8)
    for r in range(rows):
        non_zero_values=np.flatnonzero(roi[r])
        if any(non_zero_values): 
            drawing.itemset((r,non_zero_values[0]),255)

    # Rotate image to go back to the base coordinates
    if(direction[0] == -1): drawing = cv.flip(drawing, 1) 
    if(direction[1] != 0): 
        if(direction[1] == -1): drawing = cv.flip(drawing, 1) 
        drawing =  cv.rotate(drawing, cv.ROTATE_90_COUNTERCLOCKWISE)
         
    # Find outer line points 
    pts = []
    pts = cv.findNonZero(drawing)

    #showResizedImg(drawing,'findLinesPoints2',scale = 2 )     
    return pts

def searchingBox(image, points, direction=(0,1)):
    '''
    Search for straight lines in predefined areas.
    Find points belonging to the line.
    Fit line to extracted points.
    '''

    # Specify ROI
    pts = (points[0],(points[0]+points[2]),points[1],(points[1]+points[3]))
    # Apply ROI
    roi = image.copy()[pts[2]:pts[3],pts[0]:pts[1]]
    # Treshold
    ret,roi = cv.threshold(roi,150,255,cv.THRESH_TOZERO)
    # Find points with belongs to the edge
    roi = linesFiltration(roi,direction)
    pts = findLinesPoints(roi,direction)
   
    # Break in case of faulty input image
    if(pts is None):
        print("Any line found")
        return -1,-1,-1,-1  

    # Fit line
    vector = np.array(pts)
    vx,vy,x,y = cv.fitLine(vector,cv.DIST_HUBER, 0, 0.01, 0.05) 
    
    # Go back to the global coordinate system  
    x = x + points[0]   
    y = y + points[1]
    line = vx,vy,x,y

    # Draw line 
    k = 10000
    p1 = (int(x - k*vx), int(y - k * vy))
    p2 = (int(x + k*vx), int(y + k * vy))
    cv.line(img, p1,p2 , (255,255,255), 3, cv.LINE_AA, 0)
    cv.rectangle(img,(points[0],points[1],points[2],points[3]),(255,255,255),2)
    
    # Show ROI and fitted line on the orgnial image
    showResizedImg(img,'Image',scale = 0.5 )
    return line 

def findArcPoint(image,line1,line2):
    '''
    Finding centre of the cutting insert arc basing on previously calculated lines.
    Extract region with the arc. Apply polar transform to straighten it.
    Calculate mean, median and std error. 
    Decide if the insert is good or faulty (classic approach).
    '''
    
    # Solving linear equation to find lines crossing point
    vx1,vy1,x1,y1 = line1
    vx2,vy2,x2,y2 = line2
    A = np.array([[vx1, 0, -1,0], [vy1, 0, 0,-1], [0, vx2, -1,0], [0, vy2, 0,-1]], dtype='float')
    B = np.array([-x1,-y1,-x2,-y2], dtype='float')
    R = np.linalg.inv(A).dot(B)
    xs,ys = R[2:]
    rot_ang = math.atan2(vy2,vx2) 
    vy =  abs( vx1 +  vx2 ) if vy2 < 0 else abs( vy1 +  vy2 )
    vx = abs( vy1 +  vy2 ) if vy2 < 0 else abs( vx1 +  vx2 )

    l = math.sqrt(vx**2 + vy**2) # lenght of those vectors
    k = (PX2MM*4)/l # how many vectors is between line crossing point and cutting insert arc centre
    p1 = (int(xs + k*vx), int(ys + k * vy))
    p2 = (int(xs ), int(ys ))
    cv.line(img, p1,p2 , (255,255,255), 2, cv.LINE_AA, 0)

    # Find 4 possible arc centres of the cutting insert
    C = [] # coortinates of the 4 possible arc centres
    v = np.array([[vx,vy],[-vx,vy],[-vx,-vy],[vx,-vy]], dtype='float')  # All possible direction of the vectors

    for i in range(len(v)): # all possible configurations
        pom = xs + v[i][0]*k  , ys + v[i][1]*k
        cv.circle(img,(int(xs + v[i][0]*k),int(ys + v[i][1]*k)),1,(255,255,255),4) ### Visualization 
        C.append(pom)
 
    # Chose ROI with contains cutting insert arc - closest to the centre of the image
    min_dist = 9999
    img_cy,img_cx=img.shape[:2]
    for i in range(len(v)):
        dist = math.sqrt( (C[i][0]-img_cx/2)**2 +  (C[i][1]-img_cy/2)**2 )
        if(  dist < min_dist):
            min_dist = dist
            properArc = i       
    xc,yc=C[properArc] #proper arc centre coordinates

    # Build roi between arc centre (xc,yc) and lines crossing point (xs,ys) in dependece on their location 
    inc = 100 # Offset outer boundaries by some offset to avoid cutting the arc
    rx0 = int(xc) if xc < xs else int(xs-inc) 
    ry0 = int(yc) if yc < ys else int(ys-inc)
    rxk = int(xc) if xc > xs else int(xs+inc) 
    ryk = int(yc) if yc > ys else int(ys+inc)
    roi = image.copy()[ry0:ryk,rx0:rxk]

    # Rotate roi
    ang = 0
    if(xc>xs and yc<ys): ang = 90 
    elif(xc>xs and yc>ys): ang = 180 
    elif(xc<xs and yc>ys): ang = 270  
    roi = ndimage.rotate(roi, ang)

    ### Visualization ###
    cv.circle(img,(int(R[2]),int(R[3])),int(PX2MM*4),(255,255,255),3) # Lines intersection
    cv.circle(img,(int(xc),int(yc)),5,(255,255,255),3) # Arc centre
    cv.circle(img,(int(xc),int(yc)),int(PX2MM*4/math.sqrt(2)),(255,255,255),2) # Arc radius
    #showResizedImg(roi,'Arc ROI',scale = 1 ) ### Visualization 
    showResizedImg(img,'Image',scale = 0.5 ) ### Visualization 
    printTime("Find arc prep")

    # Polar transform and filtration
    try:
        roi = polarTransform(roi,start_point=(0,0),r=(int(PX2MM*1),int(PX2MM*2.25)),theta=90,theta_inc=0.25)
    except:
        roi = roi
        print("Can't find cutting insert arc")
        return -1
    #showResizedImg(roi,'Arc ROI after polar transform',scale = 2 )

    #Find edge on the image after polarTransform
    ret,roi2 = cv.threshold(roi,150,255,cv.THRESH_TOZERO)
    roi2 = linesFiltration(roi2,(0,-1))
    pts = findLinesPoints(roi2,(0,1))

    if(pts is None):
        print("Any line found")
        return -1
    else: 
        pts_y = []
        for i in range(len(pts)): pts_y.append(pts[i][0][1])
        statatistics = ExamineArc

        s = statatistics.srednia(pts_y) 
        m = statatistics.mediana(pts_y)  
        o = statatistics.odchylenie(pts_y, s)  
        #print("Średnia: {:.2f}\nMediana: {:.2f}\nOdchylenie standardowe: {:.2f}".format(s,m,o))
        if(s < MAX_DIM and s > MIN_DIM) and o < STD_ERROR:
            cv.putText(img,('OK    '+'srednia: {:.2f} odchylenie: {:.2f}').format(s,o),(100,100), cv.FONT_HERSHEY_PLAIN, 5,255,2)
        else:
            cv.putText(img,('N_OK   '+'srednia: {:.2f} odchylenie: {:.2f}').format(s,o),(100,100), cv.FONT_HERSHEY_PLAIN, 5,255,2)
    #showResizedImg(roi,'Orginal Arc',scale = 3 ) ### Visualization 
    #showResizedImg(roi2,'Binary Arc',scale = 3 ) ### Visualization 
    return 0

def polarTransform(roi,start_point,r,theta,theta_inc):
    '''
    Transform cutting inserts arc curve into polar coordinates.
    Use custom angle and range.
    '''
    
    drawing = np.zeros((roi.shape[0], roi.shape[1]), dtype=np.uint8)
    roi2 = np.zeros((int(r[1]-r[0]),int(theta/theta_inc)+1), dtype=np.uint8)
    theta_range = np.arange(0, theta, theta_inc)

    for alpha in theta_range:
        x0 = int(math.sin(math.radians(alpha))*r[0])
        y0 = int(math.cos(math.radians(alpha))*r[0])
  
        roid = cv.cvtColor(roi,cv.COLOR_GRAY2BGR)
        
        for R in range(r[0],r[1]):
            x = int(math.sin(math.radians(alpha))*R)+x0
            y = int(math.cos(math.radians(alpha))*R)+y0
        
            #roi2[R-r[0],int(alpha/theta_inc)] = roi[x,y]
            roi2.itemset( R-r[0] , int(alpha/theta_inc) , roi.item(x,y) ) 

            # Debuging and demonstrating pourposed display transforma row by row
            '''cv.circle(drawing,(x,y),1,(0,0,255),1)
            drawing = cv.bitwise_or(drawing, roid)
            showResizedImg(drawing,'Polar lines',scale = 1 ) ### Visualization 
            showResizedImg(roi2,'Polar ROI',scale = 1 ) ### Visualization 
            cv.waitKey(1)'''
    return roi2  

# Output analyze
class ExamineArc:
    '''
    Used to analyze of the image processing results
    '''

    def srednia(pts):
        suma = sum(pts)
        return suma / float(len(pts))
    
    def mediana(pts):
        pts.sort()
        if len(pts) % 2 == 0:  
            half = int(len(pts) / 2)
            return float(sum(pts[half - 1:half + 1])) / 2.0
        else: 
            return pts[int(len(pts) / 2)]

    def wariancja(pts, srednia):
        sigma = 0.0
        for ocena in pts:
            sigma += (ocena - srednia)**2
        return sigma / len(pts)

    def odchylenie(pts, srednia): 
        w = ExamineArc.wariancja(pts, srednia)
        return math.sqrt(w) 

# Deep learning clasification
def deepL(orgImg):
  
    #Define ROI 
    XC,YC = 1480,1220
    Xdim, Ydim = 1000, 600
    end_point = (XC, YC)
    start_point = (int(XC-Xdim), int(YC-Ydim))
    deepL_img = orgImg[start_point[1]:end_point[1],start_point[0]:end_point[0]]
    deepL_img = cv.resize(deepL_img, (224,224), interpolation = cv.INTER_AREA)

    # Clasification
    classification = []
    x = deepL_img.astype(np.float32)/255
    x = np.expand_dims(x, axis=0)
    image_tensor = np.vstack([x])
    classes = model.predict(image_tensor)

    if classes > 0.5:
        title =  "is good  " + str(round(float((classes)*100),2)) + "%"
    else:
        title =  "is faulty  " + str(round(float((1-classes)*100),2)) + "%"
    print(title)
    cv.putText(img,title,(100,300), cv.FONT_HERSHEY_PLAIN, 5,255,2)

# Displaying
def showResizedImg(image,windowName="Deep Learning Clasification",scale=1):
    cv.namedWindow(windowName, cv.WINDOW_NORMAL)
    cv.imshow(windowName,image)
    windowShape = (int(image.shape[1]*scale),int(image.shape[0]*scale)) 
    cv.resizeWindow(windowName,windowShape)
def printTime(str='Deep learning clasificatrion time'):
    elapsed_time = time.time() - start_time
    print("{}: \t {:.3f}s".format(str,elapsed_time))    
#------------------Functions-end-------------------#




#--------------------Main-loop---------------------#
while camera.IsGrabbing():
    grabResult = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)

    if grabResult.GrabSucceeded():
        # Access the image data
        image = converter.Convert(grabResult)
        img = image.GetArray()
    grabResult.Release()

    # Drawing rectangles on the pre-captured image for better positioning
    img3 = img.copy() # Keep clear frame for deep learning
    cv.rectangle(img,(1000,625,800,200),(255,255,255),2) # Draw positioning rectangles
    cv.rectangle(img,(325,1075,300,300),(255,255,255),2)
    showResizedImg(img,'Image',scale = 0.5 ) ### Visualization 
    img = img3.copy() # Restore clear frame
    
    # Get key from user ESC-break SPACE-process frame
    key = cv.waitKey(1)
    if key == 27:
        cv.destroyAllWindows()
        break

    # Frame processing
    if key == 32:
        start_time = time.time()

        # If there is an image convert it to grayscale
        try:
            img = cv.cvtColor(img, cv.COLOR_BGR2GRAY)
            img2 = img.copy() # Backup clear frame    
        except:
            print("Image not found")
            sys.exit(1) 
        showResizedImg(img,'Image',scale = 0.5 ) ### Visualization 
        printTime("Grabbing frame")
        
        # Detect lines
        line1 = searchingBox(img,(1000,625,800,200),(0,1))
        showResizedImg(img,'Image',scale = 0.5 ) ### Visualization 
        cv.waitKey(1)
        line2 = searchingBox(img,(325,1075,300,300),(1,0))
        showResizedImg(img,'Image',scale = 0.5 ) ### Visualization 
        cv.waitKey(1)
        printTime("Detecting lines") 

        # Find and examine edge
        findArcPoint(img2,line1,line2)
        showResizedImg(img,'Image',scale = 0.5 ) ### Visualization 
        cv.waitKey(1)
        printTime("Examine edge") 

        # DeepL clacification
        deepL(img3)
        showResizedImg(img,'Image',scale = 0.5 ) ### Visualization  
        printTime("DeepL Time")

        cv.waitKey(0)
        cv.destroyAllWindows()
#-----------------Main-loop-end--------------------#
    
# Releasing the resource    
camera.StopGrabbing()