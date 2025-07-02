# -*- coding: utf-8 -*-  
  
class QueryFormatError(Exception):  
    """Invalid Query Format."""  
      
    def __init__(self, message="Invalid query format"):  
        self.message = message  
        super().__init__(self.message)