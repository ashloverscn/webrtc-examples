import cv2

# Initialize the camera (0 is usually the default camera)
cap = cv2.VideoCapture(0)

if not cap.isOpened():
    print("Error: Could not open camera.")
else:
    print("Camera opened successfully!")
    print("Press 'q' to quit the preview.")
    
    while True:
        # Capture frame-by-frame
        ret, frame = cap.read()
        
        if not ret:
            print("Error: Can't receive frame.")
            break
        
        # Display the frame
        cv2.imshow('Camera Preview', frame)
        
        # Press 'q' to exit
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
    
    # Release the camera and close windows
    cap.release()
    cv2.destroyAllWindows()
    print("Camera preview closed.")

